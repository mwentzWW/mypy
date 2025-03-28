from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Set, Tuple, TypeVar, Union, cast

from typing_extensions import Final, TypeAlias as _TypeAlias

import mypy.applytype
import mypy.constraints
import mypy.typeops
from mypy.erasetype import erase_type
from mypy.expandtype import expand_type_by_instance
from mypy.maptype import map_instance_to_supertype

# Circular import; done in the function instead.
# import mypy.solve
from mypy.nodes import (
    CONTRAVARIANT,
    COVARIANT,
    Decorator,
    FuncBase,
    OverloadedFuncDef,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.state import state
from mypy.types import (
    TUPLE_LIKE_INSTANCE_NAMES,
    TYPED_NAMEDTUPLE_NAMES,
    AnyType,
    CallableType,
    DeletedType,
    ErasedType,
    FormalArgument,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecType,
    PartialType,
    ProperType,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeOfAny,
    TypeType,
    TypeVarTupleType,
    TypeVarType,
    TypeVisitor,
    UnboundType,
    UninhabitedType,
    UnionType,
    UnpackType,
    get_proper_type,
    is_named_instance,
)
from mypy.typestate import SubtypeKind, TypeState
from mypy.typevartuples import extract_unpack, split_with_instance

# Flags for detected protocol members
IS_SETTABLE: Final = 1
IS_CLASSVAR: Final = 2
IS_CLASS_OR_STATIC: Final = 3

TypeParameterChecker: _TypeAlias = Callable[[Type, Type, int, bool], bool]


class SubtypeContext:
    def __init__(
        self,
        *,
        # Non-proper subtype flags
        ignore_type_params: bool = False,
        ignore_pos_arg_names: bool = False,
        ignore_declared_variance: bool = False,
        # Supported for both proper and non-proper
        ignore_promotions: bool = False,
        # Proper subtype flags
        erase_instances: bool = False,
        keep_erased_types: bool = False,
        options: Optional[Options] = None,
    ) -> None:
        self.ignore_type_params = ignore_type_params
        self.ignore_pos_arg_names = ignore_pos_arg_names
        self.ignore_declared_variance = ignore_declared_variance
        self.ignore_promotions = ignore_promotions
        self.erase_instances = erase_instances
        self.keep_erased_types = keep_erased_types
        self.options = options

    def check_context(self, proper_subtype: bool) -> None:
        # Historically proper and non-proper subtypes were defined using different helpers
        # and different visitors. Check if flag values are such that we definitely support.
        if proper_subtype:
            assert (
                not self.ignore_type_params
                and not self.ignore_pos_arg_names
                and not self.ignore_declared_variance
            )
        else:
            assert not self.erase_instances and not self.keep_erased_types


def is_subtype(
    left: Type,
    right: Type,
    *,
    subtype_context: Optional[SubtypeContext] = None,
    ignore_type_params: bool = False,
    ignore_pos_arg_names: bool = False,
    ignore_declared_variance: bool = False,
    ignore_promotions: bool = False,
    options: Optional[Options] = None,
) -> bool:
    """Is 'left' subtype of 'right'?

    Also consider Any to be a subtype of any type, and vice versa. This
    recursively applies to components of composite types (List[int] is subtype
    of List[Any], for example).

    type_parameter_checker is used to check the type parameters (for example,
    A with B in is_subtype(C[A], C[B]). The default checks for subtype relation
    between the type arguments (e.g., A and B), taking the variance of the
    type var into account.
    """
    if subtype_context is None:
        subtype_context = SubtypeContext(
            ignore_type_params=ignore_type_params,
            ignore_pos_arg_names=ignore_pos_arg_names,
            ignore_declared_variance=ignore_declared_variance,
            ignore_promotions=ignore_promotions,
            options=options,
        )
    else:
        assert not any(
            {
                ignore_type_params,
                ignore_pos_arg_names,
                ignore_declared_variance,
                ignore_promotions,
                options,
            }
        ), "Don't pass both context and individual flags"
    if TypeState.is_assumed_subtype(left, right):
        return True
    if mypy.typeops.is_recursive_pair(left, right):
        # This case requires special care because it may cause infinite recursion.
        # Our view on recursive types is known under a fancy name of iso-recursive mu-types.
        # Roughly this means that a recursive type is defined as an alias where right hand side
        # can refer to the type as a whole, for example:
        #     A = Union[int, Tuple[A, ...]]
        # and an alias unrolled once represents the *same type*, in our case all these represent
        # the same type:
        #    A
        #    Union[int, Tuple[A, ...]]
        #    Union[int, Tuple[Union[int, Tuple[A, ...]], ...]]
        # The algorithm for subtyping is then essentially under the assumption that left <: right,
        # check that get_proper_type(left) <: get_proper_type(right). On the example above,
        # If we start with:
        #     A = Union[int, Tuple[A, ...]]
        #     B = Union[int, Tuple[B, ...]]
        # When checking if A <: B we push pair (A, B) onto 'assuming' stack, then when after few
        # steps we come back to initial call is_subtype(A, B) and immediately return True.
        with pop_on_exit(TypeState.get_assumptions(is_proper=False), left, right):
            return _is_subtype(left, right, subtype_context, proper_subtype=False)
    return _is_subtype(left, right, subtype_context, proper_subtype=False)


def is_proper_subtype(
    left: Type,
    right: Type,
    *,
    subtype_context: Optional[SubtypeContext] = None,
    ignore_promotions: bool = False,
    erase_instances: bool = False,
    keep_erased_types: bool = False,
) -> bool:
    """Is left a proper subtype of right?

    For proper subtypes, there's no need to rely on compatibility due to
    Any types. Every usable type is a proper subtype of itself.

    If erase_instances is True, erase left instance *after* mapping it to supertype
    (this is useful for runtime isinstance() checks). If keep_erased_types is True,
    do not consider ErasedType a subtype of all types (used by type inference against unions).
    """
    if subtype_context is None:
        subtype_context = SubtypeContext(
            ignore_promotions=ignore_promotions,
            erase_instances=erase_instances,
            keep_erased_types=keep_erased_types,
        )
    else:
        assert not any(
            {ignore_promotions, erase_instances, keep_erased_types}
        ), "Don't pass both context and individual flags"
    if TypeState.is_assumed_proper_subtype(left, right):
        return True
    if mypy.typeops.is_recursive_pair(left, right):
        # Same as for non-proper subtype, see detailed comment there for explanation.
        with pop_on_exit(TypeState.get_assumptions(is_proper=True), left, right):
            return _is_subtype(left, right, subtype_context, proper_subtype=True)
    return _is_subtype(left, right, subtype_context, proper_subtype=True)


def is_equivalent(
    a: Type,
    b: Type,
    *,
    ignore_type_params: bool = False,
    ignore_pos_arg_names: bool = False,
    options: Optional[Options] = None,
) -> bool:
    return is_subtype(
        a,
        b,
        ignore_type_params=ignore_type_params,
        ignore_pos_arg_names=ignore_pos_arg_names,
        options=options,
    ) and is_subtype(
        b,
        a,
        ignore_type_params=ignore_type_params,
        ignore_pos_arg_names=ignore_pos_arg_names,
        options=options,
    )


def is_same_type(a: Type, b: Type, ignore_promotions: bool = True) -> bool:
    """Are these types proper subtypes of each other?

    This means types may have different representation (e.g. an alias, or
    a non-simplified union) but are semantically exchangeable in all contexts.
    """
    # Note that using ignore_promotions=True (default) makes types like int and int64
    # considered not the same type (which is the case at runtime).
    # Also Union[bool, int] (if it wasn't simplified before) will be different
    # from plain int, etc.
    return is_proper_subtype(a, b, ignore_promotions=ignore_promotions) and is_proper_subtype(
        b, a, ignore_promotions=ignore_promotions
    )


# This is a common entry point for subtyping checks (both proper and non-proper).
# Never call this private function directly, use the public versions.
def _is_subtype(
    left: Type, right: Type, subtype_context: SubtypeContext, proper_subtype: bool
) -> bool:
    subtype_context.check_context(proper_subtype)
    orig_right = right
    orig_left = left
    left = get_proper_type(left)
    right = get_proper_type(right)

    if not proper_subtype and (
        isinstance(right, AnyType)
        or isinstance(right, UnboundType)
        or isinstance(right, ErasedType)
    ):
        # TODO: should we consider all types proper subtypes of UnboundType and/or
        # ErasedType as we do for non-proper subtyping.
        return True

    def check_item(left: Type, right: Type, subtype_context: SubtypeContext) -> bool:
        if proper_subtype:
            return is_proper_subtype(left, right, subtype_context=subtype_context)
        return is_subtype(left, right, subtype_context=subtype_context)

    if isinstance(right, UnionType) and not isinstance(left, UnionType):
        # Normally, when 'left' is not itself a union, the only way
        # 'left' can be a subtype of the union 'right' is if it is a
        # subtype of one of the items making up the union.
        is_subtype_of_item = any(
            check_item(orig_left, item, subtype_context) for item in right.items
        )
        # Recombine rhs literal types, to make an enum type a subtype
        # of a union of all enum items as literal types. Only do it if
        # the previous check didn't succeed, since recombining can be
        # expensive.
        # `bool` is a special case, because `bool` is `Literal[True, False]`.
        if (
            not is_subtype_of_item
            and isinstance(left, Instance)
            and (left.type.is_enum or left.type.fullname == "builtins.bool")
        ):
            right = UnionType(mypy.typeops.try_contracting_literals_in_union(right.items))
            is_subtype_of_item = any(
                check_item(orig_left, item, subtype_context) for item in right.items
            )
        # However, if 'left' is a type variable T, T might also have
        # an upper bound which is itself a union. This case will be
        # handled below by the SubtypeVisitor. We have to check both
        # possibilities, to handle both cases like T <: Union[T, U]
        # and cases like T <: B where B is the upper bound of T and is
        # a union. (See #2314.)
        if not isinstance(left, TypeVarType):
            return is_subtype_of_item
        elif is_subtype_of_item:
            return True
        # otherwise, fall through
    return left.accept(SubtypeVisitor(orig_right, subtype_context, proper_subtype))


# TODO: should we pass on the original flags here and in couple other places?
# This seems logical but was never done in the past for some reasons.
def check_type_parameter(lefta: Type, righta: Type, variance: int, proper_subtype: bool) -> bool:
    def check(left: Type, right: Type) -> bool:
        return is_proper_subtype(left, right) if proper_subtype else is_subtype(left, right)

    if variance == COVARIANT:
        return check(lefta, righta)
    elif variance == CONTRAVARIANT:
        return check(righta, lefta)
    else:
        if proper_subtype:
            return is_same_type(lefta, righta)
        return is_equivalent(lefta, righta)


def ignore_type_parameter(lefta: Type, righta: Type, variance: int, proper_subtype: bool) -> bool:
    return True


class SubtypeVisitor(TypeVisitor[bool]):
    def __init__(self, right: Type, subtype_context: SubtypeContext, proper_subtype: bool) -> None:
        self.right = get_proper_type(right)
        self.orig_right = right
        self.proper_subtype = proper_subtype
        self.subtype_context = subtype_context
        self.check_type_parameter = (
            ignore_type_parameter if subtype_context.ignore_type_params else check_type_parameter
        )
        self.options = subtype_context.options
        self._subtype_kind = SubtypeVisitor.build_subtype_kind(subtype_context, proper_subtype)

    @staticmethod
    def build_subtype_kind(subtype_context: SubtypeContext, proper_subtype: bool) -> SubtypeKind:
        return (
            state.strict_optional,
            proper_subtype,
            subtype_context.ignore_type_params,
            subtype_context.ignore_pos_arg_names,
            subtype_context.ignore_declared_variance,
            subtype_context.ignore_promotions,
            subtype_context.erase_instances,
            subtype_context.keep_erased_types,
        )

    def _is_subtype(self, left: Type, right: Type) -> bool:
        if self.proper_subtype:
            return is_proper_subtype(left, right, subtype_context=self.subtype_context)
        return is_subtype(left, right, subtype_context=self.subtype_context)

    # visit_x(left) means: is left (which is an instance of X) a subtype of
    # right?

    def visit_unbound_type(self, left: UnboundType) -> bool:
        # This can be called if there is a bad type annotation. The result probably
        # doesn't matter much but by returning True we simplify these bad types away
        # from unions, which could filter out some bogus messages.
        return True

    def visit_any(self, left: AnyType) -> bool:
        return isinstance(self.right, AnyType) if self.proper_subtype else True

    def visit_none_type(self, left: NoneType) -> bool:
        if state.strict_optional:
            if isinstance(self.right, NoneType) or is_named_instance(
                self.right, "builtins.object"
            ):
                return True
            if isinstance(self.right, Instance) and self.right.type.is_protocol:
                members = self.right.type.protocol_members
                # None is compatible with Hashable (and other similar protocols). This is
                # slightly sloppy since we don't check the signature of "__hash__".
                # None is also compatible with `SupportsStr` protocol.
                return not members or all(member in ("__hash__", "__str__") for member in members)
            return False
        else:
            return True

    def visit_uninhabited_type(self, left: UninhabitedType) -> bool:
        return True

    def visit_erased_type(self, left: ErasedType) -> bool:
        # This may be encountered during type inference. The result probably doesn't
        # matter much.
        # TODO: it actually does matter, figure out more principled logic about this.
        if self.subtype_context.keep_erased_types:
            return False
        return True

    def visit_deleted_type(self, left: DeletedType) -> bool:
        return True

    def visit_instance(self, left: Instance) -> bool:
        if left.type.fallback_to_any and not self.proper_subtype:
            if isinstance(self.right, NoneType):
                # NOTE: `None` is a *non-subclassable* singleton, therefore no class
                # can by a subtype of it, even with an `Any` fallback.
                # This special case is needed to treat descriptors in classes with
                # dynamic base classes correctly, see #5456.
                return False
            return True
        right = self.right
        if isinstance(right, TupleType) and mypy.typeops.tuple_fallback(right).type.is_enum:
            return self._is_subtype(left, mypy.typeops.tuple_fallback(right))
        if isinstance(right, Instance):
            if TypeState.is_cached_subtype_check(self._subtype_kind, left, right):
                return True
            if not self.subtype_context.ignore_promotions:
                for base in left.type.mro:
                    if base._promote and any(
                        self._is_subtype(p, self.right) for p in base._promote
                    ):
                        TypeState.record_subtype_cache_entry(self._subtype_kind, left, right)
                        return True
                # Special case: Low-level integer types are compatible with 'int'. We can't
                # use promotions, since 'int' is already promoted to low-level integer types,
                # and we can't have circular promotions.
                if left.type.alt_promote is right.type:
                    return True
            rname = right.type.fullname
            # Always try a nominal check if possible,
            # there might be errors that a user wants to silence *once*.
            # NamedTuples are a special case, because `NamedTuple` is not listed
            # in `TypeInfo.mro`, so when `(a: NamedTuple) -> None` is used,
            # we need to check for `is_named_tuple` property
            if (
                left.type.has_base(rname)
                or rname == "builtins.object"
                or (
                    rname in TYPED_NAMEDTUPLE_NAMES
                    and any(l.is_named_tuple for l in left.type.mro)
                )
            ) and not self.subtype_context.ignore_declared_variance:
                # Map left type to corresponding right instances.
                t = map_instance_to_supertype(left, right.type)
                if self.subtype_context.erase_instances:
                    erased = erase_type(t)
                    assert isinstance(erased, Instance)
                    t = erased
                nominal = True
                if right.type.has_type_var_tuple_type:
                    left_prefix, left_middle, left_suffix = split_with_instance(left)
                    right_prefix, right_middle, right_suffix = split_with_instance(right)

                    left_unpacked = extract_unpack(left_middle)
                    right_unpacked = extract_unpack(right_middle)

                    # Helper for case 2 below so we can treat them the same.
                    def check_mixed(
                        unpacked_type: ProperType, compare_to: Tuple[Type, ...]
                    ) -> bool:
                        if isinstance(unpacked_type, TypeVarTupleType):
                            return False
                        if isinstance(unpacked_type, AnyType):
                            return True
                        if isinstance(unpacked_type, TupleType):
                            if len(unpacked_type.items) != len(compare_to):
                                return False
                            for t1, t2 in zip(unpacked_type.items, compare_to):
                                if not is_equivalent(t1, t2):
                                    return False
                            return True
                        return False

                    # Case 1: Both are unpacks, in this case we check what is being
                    # unpacked is the same.
                    if left_unpacked is not None and right_unpacked is not None:
                        if not is_equivalent(left_unpacked, right_unpacked):
                            return False

                    # Case 2: Only one of the types is an unpack. The equivalence
                    # case is mostly the same but we check some additional
                    # things when unpacking on the right.
                    elif left_unpacked is not None and right_unpacked is None:
                        if not check_mixed(left_unpacked, right_middle):
                            return False
                    elif left_unpacked is None and right_unpacked is not None:
                        if (
                            isinstance(right_unpacked, Instance)
                            and right_unpacked.type.fullname == "builtins.tuple"
                        ):
                            return all(
                                is_equivalent(l, right_unpacked.args[0]) for l in left_middle
                            )
                        if not check_mixed(right_unpacked, left_middle):
                            return False

                    # Case 3: Neither type is an unpack. In this case we just compare
                    # the items themselves.
                    else:
                        if len(left_middle) != len(right_middle):
                            return False
                        for left_t, right_t in zip(left_middle, right_middle):
                            if not is_equivalent(left_t, right_t):
                                return False

                    left_items = t.args[: right.type.type_var_tuple_prefix]
                    right_items = right.args[: right.type.type_var_tuple_prefix]
                    if right.type.type_var_tuple_suffix:
                        left_items += t.args[-right.type.type_var_tuple_suffix :]
                        right_items += right.args[-right.type.type_var_tuple_suffix :]

                    unpack_index = right.type.type_var_tuple_prefix
                    assert unpack_index is not None
                    type_params = zip(
                        left_prefix + right_suffix,
                        right_prefix + right_suffix,
                        right.type.defn.type_vars[:unpack_index]
                        + right.type.defn.type_vars[unpack_index + 1 :],
                    )
                else:
                    type_params = zip(t.args, right.args, right.type.defn.type_vars)
                for lefta, righta, tvar in type_params:
                    if isinstance(tvar, TypeVarType):
                        if not self.check_type_parameter(
                            lefta, righta, tvar.variance, self.proper_subtype
                        ):
                            nominal = False
                    else:
                        if not self.check_type_parameter(
                            lefta, righta, COVARIANT, self.proper_subtype
                        ):
                            nominal = False
                if nominal:
                    TypeState.record_subtype_cache_entry(self._subtype_kind, left, right)
                return nominal
            if right.type.is_protocol and is_protocol_implementation(
                left, right, proper_subtype=self.proper_subtype
            ):
                return True
            return False
        if isinstance(right, TypeType):
            item = right.item
            if isinstance(item, TupleType):
                item = mypy.typeops.tuple_fallback(item)
            # TODO: this is a bit arbitrary, we should only skip Any-related cases.
            if not self.proper_subtype:
                if is_named_instance(left, "builtins.type"):
                    return self._is_subtype(TypeType(AnyType(TypeOfAny.special_form)), right)
                if left.type.is_metaclass():
                    if isinstance(item, AnyType):
                        return True
                    if isinstance(item, Instance):
                        return is_named_instance(item, "builtins.object")
        if isinstance(right, LiteralType) and left.last_known_value is not None:
            return self._is_subtype(left.last_known_value, right)
        if isinstance(right, CallableType):
            # Special case: Instance can be a subtype of Callable.
            call = find_member("__call__", left, left, is_operator=True)
            if call:
                return self._is_subtype(call, right)
            return False
        else:
            return False

    def visit_type_var(self, left: TypeVarType) -> bool:
        right = self.right
        if isinstance(right, TypeVarType) and left.id == right.id:
            return True
        if left.values and self._is_subtype(UnionType.make_union(left.values), right):
            return True
        return self._is_subtype(left.upper_bound, self.right)

    def visit_param_spec(self, left: ParamSpecType) -> bool:
        right = self.right
        if (
            isinstance(right, ParamSpecType)
            and right.id == left.id
            and right.flavor == left.flavor
        ):
            return True
        return self._is_subtype(left.upper_bound, self.right)

    def visit_type_var_tuple(self, left: TypeVarTupleType) -> bool:
        right = self.right
        if isinstance(right, TypeVarTupleType) and right.id == left.id:
            return True
        return self._is_subtype(left.upper_bound, self.right)

    def visit_unpack_type(self, left: UnpackType) -> bool:
        if isinstance(self.right, UnpackType):
            return self._is_subtype(left.type, self.right.type)
        return False

    def visit_parameters(self, left: Parameters) -> bool:
        right = self.right
        if isinstance(right, Parameters) or isinstance(right, CallableType):
            return are_parameters_compatible(
                left,
                right,
                is_compat=self._is_subtype,
                ignore_pos_arg_names=self.subtype_context.ignore_pos_arg_names,
            )
        else:
            return False

    def visit_callable_type(self, left: CallableType) -> bool:
        right = self.right
        if isinstance(right, CallableType):
            if left.type_guard is not None and right.type_guard is not None:
                if not self._is_subtype(left.type_guard, right.type_guard):
                    return False
            elif right.type_guard is not None and left.type_guard is None:
                # This means that one function has `TypeGuard` and other does not.
                # They are not compatible. See https://github.com/python/mypy/issues/11307
                return False
            return is_callable_compatible(
                left,
                right,
                is_compat=self._is_subtype,
                ignore_pos_arg_names=self.subtype_context.ignore_pos_arg_names,
                strict_concatenate=self.options.strict_concatenate if self.options else True,
            )
        elif isinstance(right, Overloaded):
            return all(self._is_subtype(left, item) for item in right.items)
        elif isinstance(right, Instance):
            if right.type.is_protocol and right.type.protocol_members == ["__call__"]:
                # OK, a callable can implement a protocol with a single `__call__` member.
                # TODO: we should probably explicitly exclude self-types in this case.
                call = find_member("__call__", right, left, is_operator=True)
                assert call is not None
                if self._is_subtype(left, call):
                    return True
            return self._is_subtype(left.fallback, right)
        elif isinstance(right, TypeType):
            # This is unsound, we don't check the __init__ signature.
            return left.is_type_obj() and self._is_subtype(left.ret_type, right.item)
        elif isinstance(right, Parameters):
            # this doesn't check return types.... but is needed for is_equivalent
            return are_parameters_compatible(
                left,
                right,
                is_compat=self._is_subtype,
                ignore_pos_arg_names=self.subtype_context.ignore_pos_arg_names,
            )
        else:
            return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if is_named_instance(right, "typing.Sized"):
                return True
            elif is_named_instance(right, TUPLE_LIKE_INSTANCE_NAMES):
                if right.args:
                    iter_type = right.args[0]
                else:
                    if self.proper_subtype:
                        return False
                    iter_type = AnyType(TypeOfAny.special_form)
                if is_named_instance(right, "builtins.tuple") and isinstance(
                    get_proper_type(iter_type), AnyType
                ):
                    # TODO: We shouldn't need this special case. This is currently needed
                    #       for isinstance(x, tuple), though it's unclear why.
                    return True
                return all(self._is_subtype(li, iter_type) for li in left.items)
            elif self._is_subtype(mypy.typeops.tuple_fallback(left), right):
                return True
            return False
        elif isinstance(right, TupleType):
            if len(left.items) != len(right.items):
                return False
            for l, r in zip(left.items, right.items):
                if not self._is_subtype(l, r):
                    return False
            rfallback = mypy.typeops.tuple_fallback(right)
            if is_named_instance(rfallback, "builtins.tuple"):
                # No need to verify fallback. This is useful since the calculated fallback
                # may be inconsistent due to how we calculate joins between unions vs.
                # non-unions. For example, join(int, str) == object, whereas
                # join(Union[int, C], Union[str, C]) == Union[int, str, C].
                return True
            lfallback = mypy.typeops.tuple_fallback(left)
            if not self._is_subtype(lfallback, rfallback):
                return False
            return True
        else:
            return False

    def visit_typeddict_type(self, left: TypedDictType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            return self._is_subtype(left.fallback, right)
        elif isinstance(right, TypedDictType):
            if not left.names_are_wider_than(right):
                return False
            for name, l, r in left.zip(right):
                if self.proper_subtype:
                    check = is_same_type(l, r)
                else:
                    check = is_equivalent(
                        l,
                        r,
                        ignore_type_params=self.subtype_context.ignore_type_params,
                        options=self.options,
                    )
                if not check:
                    return False
                # Non-required key is not compatible with a required key since
                # indexing may fail unexpectedly if a required key is missing.
                # Required key is not compatible with a non-required key since
                # the prior doesn't support 'del' but the latter should support
                # it.
                #
                # NOTE: 'del' support is currently not implemented (#3550). We
                #       don't want to have to change subtyping after 'del' support
                #       lands so here we are anticipating that change.
                if (name in left.required_keys) != (name in right.required_keys):
                    return False
            # (NOTE: Fallbacks don't matter.)
            return True
        else:
            return False

    def visit_literal_type(self, left: LiteralType) -> bool:
        if isinstance(self.right, LiteralType):
            return left == self.right
        else:
            return self._is_subtype(left.fallback, self.right)

    def visit_overloaded(self, left: Overloaded) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if right.type.is_protocol and right.type.protocol_members == ["__call__"]:
                # same as for CallableType
                call = find_member("__call__", right, left, is_operator=True)
                assert call is not None
                if self._is_subtype(left, call):
                    return True
            return self._is_subtype(left.fallback, right)
        elif isinstance(right, CallableType):
            for item in left.items:
                if self._is_subtype(item, right):
                    return True
            return False
        elif isinstance(right, Overloaded):
            if left == self.right:
                # When it is the same overload, then the types are equal.
                return True

            # Ensure each overload in the right side (the supertype) is accounted for.
            previous_match_left_index = -1
            matched_overloads = set()
            possible_invalid_overloads = set()

            for right_index, right_item in enumerate(right.items):
                found_match = False

                for left_index, left_item in enumerate(left.items):
                    subtype_match = self._is_subtype(left_item, right_item)

                    # Order matters: we need to make sure that the index of
                    # this item is at least the index of the previous one.
                    if subtype_match and previous_match_left_index <= left_index:
                        if not found_match:
                            # Update the index of the previous match.
                            previous_match_left_index = left_index
                            found_match = True
                            matched_overloads.add(left_item)
                            possible_invalid_overloads.discard(left_item)
                    else:
                        # If this one overlaps with the supertype in any way, but it wasn't
                        # an exact match, then it's a potential error.
                        strict_concat = self.options.strict_concatenate if self.options else True
                        if is_callable_compatible(
                            left_item,
                            right_item,
                            is_compat=self._is_subtype,
                            ignore_return=True,
                            ignore_pos_arg_names=self.subtype_context.ignore_pos_arg_names,
                            strict_concatenate=strict_concat,
                        ) or is_callable_compatible(
                            right_item,
                            left_item,
                            is_compat=self._is_subtype,
                            ignore_return=True,
                            ignore_pos_arg_names=self.subtype_context.ignore_pos_arg_names,
                            strict_concatenate=strict_concat,
                        ):
                            # If this is an overload that's already been matched, there's no
                            # problem.
                            if left_item not in matched_overloads:
                                possible_invalid_overloads.add(left_item)

                if not found_match:
                    return False

            if possible_invalid_overloads:
                # There were potentially invalid overloads that were never matched to the
                # supertype.
                return False
            return True
        elif isinstance(right, UnboundType):
            return True
        elif isinstance(right, TypeType):
            # All the items must have the same type object status, so
            # it's sufficient to query only (any) one of them.
            # This is unsound, we don't check all the __init__ signatures.
            return left.is_type_obj() and self._is_subtype(left.items[0], right)
        else:
            return False

    def visit_union_type(self, left: UnionType) -> bool:
        if isinstance(self.right, Instance):
            literal_types: Set[Instance] = set()
            # avoid redundant check for union of literals
            for item in left.relevant_items():
                p_item = get_proper_type(item)
                lit_type = mypy.typeops.simple_literal_type(p_item)
                if lit_type is not None:
                    if lit_type in literal_types:
                        continue
                    literal_types.add(lit_type)
                    item = lit_type
                if not self._is_subtype(item, self.orig_right):
                    return False
            return True
        return all(self._is_subtype(item, self.orig_right) for item in left.items)

    def visit_partial_type(self, left: PartialType) -> bool:
        # This is indeterminate as we don't really know the complete type yet.
        if self.proper_subtype:
            # TODO: What's the right thing to do here?
            return False
        if left.type is None:
            # Special case, partial `None`. This might happen when defining
            # class-level attributes with explicit `None`.
            # We can still recover from this.
            # https://github.com/python/mypy/issues/11105
            return self.visit_none_type(NoneType())
        raise RuntimeError(f'Partial type "{left}" cannot be checked with "issubtype()"')

    def visit_type_type(self, left: TypeType) -> bool:
        right = self.right
        if isinstance(right, TypeType):
            return self._is_subtype(left.item, right.item)
        if isinstance(right, CallableType):
            # This is unsound, we don't check the __init__ signature.
            return self._is_subtype(left.item, right.ret_type)
        if isinstance(right, Instance):
            if right.type.fullname in ["builtins.object", "builtins.type"]:
                # TODO: Strictly speaking, the type builtins.type is considered equivalent to
                #       Type[Any]. However, this would break the is_proper_subtype check in
                #       conditional_types for cases like isinstance(x, type) when the type
                #       of x is Type[int]. It's unclear what's the right way to address this.
                return True
            item = left.item
            if isinstance(item, TypeVarType):
                item = get_proper_type(item.upper_bound)
            if isinstance(item, Instance):
                metaclass = item.type.metaclass_type
                return metaclass is not None and self._is_subtype(metaclass, right)
        return False

    def visit_type_alias_type(self, left: TypeAliasType) -> bool:
        assert False, f"This should be never called, got {left}"


T = TypeVar("T", bound=Type)


@contextmanager
def pop_on_exit(stack: List[Tuple[T, T]], left: T, right: T) -> Iterator[None]:
    stack.append((left, right))
    yield
    stack.pop()


def is_protocol_implementation(
    left: Instance, right: Instance, proper_subtype: bool = False
) -> bool:
    """Check whether 'left' implements the protocol 'right'.

    If 'proper_subtype' is True, then check for a proper subtype.
    Treat recursive protocols by using the 'assuming' structural subtype matrix
    (in sparse representation, i.e. as a list of pairs (subtype, supertype)),
    see also comment in nodes.TypeInfo. When we enter a check for classes
    (A, P), defined as following::

      class P(Protocol):
          def f(self) -> P: ...
      class A:
          def f(self) -> A: ...

    this results in A being a subtype of P without infinite recursion.
    On every false result, we pop the assumption, thus avoiding an infinite recursion
    as well.
    """
    assert right.type.is_protocol
    # We need to record this check to generate protocol fine-grained dependencies.
    TypeState.record_protocol_subtype_check(left.type, right.type)
    # nominal subtyping currently ignores '__init__' and '__new__' signatures
    members_not_to_check = {"__init__", "__new__"}
    # Trivial check that circumvents the bug described in issue 9771:
    if left.type.is_protocol:
        members_right = set(right.type.protocol_members) - members_not_to_check
        members_left = set(left.type.protocol_members) - members_not_to_check
        if not members_right.issubset(members_left):
            return False
    assuming = right.type.assuming_proper if proper_subtype else right.type.assuming
    for (l, r) in reversed(assuming):
        if l == left and r == right:
            return True
    with pop_on_exit(assuming, left, right):
        for member in right.type.protocol_members:
            if member in members_not_to_check:
                continue
            ignore_names = member != "__call__"  # __call__ can be passed kwargs
            # The third argument below indicates to what self type is bound.
            # We always bind self to the subtype. (Similarly to nominal types).
            supertype = get_proper_type(find_member(member, right, left))
            assert supertype is not None
            subtype = get_proper_type(find_member(member, left, left))
            # Useful for debugging:
            # print(member, 'of', left, 'has type', subtype)
            # print(member, 'of', right, 'has type', supertype)
            if not subtype:
                return False
            if isinstance(subtype, PartialType):
                subtype = (
                    NoneType()
                    if subtype.type is None
                    else Instance(
                        subtype.type,
                        [AnyType(TypeOfAny.unannotated)] * len(subtype.type.type_vars),
                    )
                )
            if not proper_subtype:
                # Nominal check currently ignores arg names
                # NOTE: If we ever change this, be sure to also change the call to
                # SubtypeVisitor.build_subtype_kind(...) down below.
                is_compat = is_subtype(subtype, supertype, ignore_pos_arg_names=ignore_names)
            else:
                is_compat = is_proper_subtype(subtype, supertype)
            if not is_compat:
                return False
            if isinstance(subtype, NoneType) and isinstance(supertype, CallableType):
                # We want __hash__ = None idiom to work even without --strict-optional
                return False
            subflags = get_member_flags(member, left.type)
            superflags = get_member_flags(member, right.type)
            if IS_SETTABLE in superflags:
                # Check opposite direction for settable attributes.
                if not is_subtype(supertype, subtype):
                    return False
            if (IS_CLASSVAR in subflags) != (IS_CLASSVAR in superflags):
                return False
            if IS_SETTABLE in superflags and IS_SETTABLE not in subflags:
                return False
            # This rule is copied from nominal check in checker.py
            if IS_CLASS_OR_STATIC in superflags and IS_CLASS_OR_STATIC not in subflags:
                return False

    if not proper_subtype:
        # Nominal check currently ignores arg names, but __call__ is special for protocols
        ignore_names = right.type.protocol_members != ["__call__"]
    else:
        ignore_names = False
    subtype_kind = SubtypeVisitor.build_subtype_kind(
        subtype_context=SubtypeContext(ignore_pos_arg_names=ignore_names),
        proper_subtype=proper_subtype,
    )
    TypeState.record_subtype_cache_entry(subtype_kind, left, right)
    return True


def find_member(
    name: str, itype: Instance, subtype: Type, is_operator: bool = False
) -> Optional[Type]:
    """Find the type of member by 'name' in 'itype's TypeInfo.

    Find the member type after applying type arguments from 'itype', and binding
    'self' to 'subtype'. Return None if member was not found.
    """
    # TODO: this code shares some logic with checkmember.analyze_member_access,
    # consider refactoring.
    info = itype.type
    method = info.get_method(name)
    if method:
        if isinstance(method, Decorator):
            return find_node_type(method.var, itype, subtype)
        if method.is_property:
            assert isinstance(method, OverloadedFuncDef)
            dec = method.items[0]
            assert isinstance(dec, Decorator)
            return find_node_type(dec.var, itype, subtype)
        return find_node_type(method, itype, subtype)
    else:
        # don't have such method, maybe variable or decorator?
        node = info.get(name)
        v = node.node if node else None
        if isinstance(v, Var):
            return find_node_type(v, itype, subtype)
        if (
            not v
            and name not in ["__getattr__", "__setattr__", "__getattribute__"]
            and not is_operator
        ):
            for method_name in ("__getattribute__", "__getattr__"):
                # Normally, mypy assumes that instances that define __getattr__ have all
                # attributes with the corresponding return type. If this will produce
                # many false negatives, then this could be prohibited for
                # structural subtyping.
                method = info.get_method(method_name)
                if method and method.info.fullname != "builtins.object":
                    if isinstance(method, Decorator):
                        getattr_type = get_proper_type(find_node_type(method.var, itype, subtype))
                    else:
                        getattr_type = get_proper_type(find_node_type(method, itype, subtype))
                    if isinstance(getattr_type, CallableType):
                        return getattr_type.ret_type
                    return getattr_type
        if itype.type.fallback_to_any:
            return AnyType(TypeOfAny.special_form)
    return None


def get_member_flags(name: str, info: TypeInfo) -> Set[int]:
    """Detect whether a member 'name' is settable, whether it is an
    instance or class variable, and whether it is class or static method.

    The flags are defined as following:
    * IS_SETTABLE: whether this attribute can be set, not set for methods and
      non-settable properties;
    * IS_CLASSVAR: set if the variable is annotated as 'x: ClassVar[t]';
    * IS_CLASS_OR_STATIC: set for methods decorated with @classmethod or
      with @staticmethod.
    """
    method = info.get_method(name)
    setattr_meth = info.get_method("__setattr__")
    if method:
        if isinstance(method, Decorator):
            if method.var.is_staticmethod or method.var.is_classmethod:
                return {IS_CLASS_OR_STATIC}
        elif method.is_property:  # this could be settable property
            assert isinstance(method, OverloadedFuncDef)
            dec = method.items[0]
            assert isinstance(dec, Decorator)
            if dec.var.is_settable_property or setattr_meth:
                return {IS_SETTABLE}
        return set()
    node = info.get(name)
    if not node:
        if setattr_meth:
            return {IS_SETTABLE}
        return set()
    v = node.node
    # just a variable
    if isinstance(v, Var) and not v.is_property:
        flags = {IS_SETTABLE}
        if v.is_classvar:
            flags.add(IS_CLASSVAR)
        return flags
    return set()


def find_node_type(node: Union[Var, FuncBase], itype: Instance, subtype: Type) -> Type:
    """Find type of a variable or method 'node' (maybe also a decorated method).
    Apply type arguments from 'itype', and bind 'self' to 'subtype'.
    """
    from mypy.typeops import bind_self

    if isinstance(node, FuncBase):
        typ: Optional[Type] = mypy.typeops.function_type(
            node, fallback=Instance(itype.type.mro[-1], [])
        )
    else:
        typ = node.type
    p_typ = get_proper_type(typ)
    if typ is None:
        return AnyType(TypeOfAny.from_error)
    # We don't need to bind 'self' for static methods, since there is no 'self'.
    if isinstance(node, FuncBase) or (
        isinstance(p_typ, FunctionLike)
        and node.is_initialized_in_class
        and not node.is_staticmethod
    ):
        assert isinstance(p_typ, FunctionLike)
        signature = bind_self(
            p_typ, subtype, is_classmethod=isinstance(node, Var) and node.is_classmethod
        )
        if node.is_property:
            assert isinstance(signature, CallableType)
            typ = signature.ret_type
        else:
            typ = signature
    itype = map_instance_to_supertype(itype, node.info)
    typ = expand_type_by_instance(typ, itype)
    return typ


def non_method_protocol_members(tp: TypeInfo) -> List[str]:
    """Find all non-callable members of a protocol."""

    assert tp.is_protocol
    result: List[str] = []
    anytype = AnyType(TypeOfAny.special_form)
    instance = Instance(tp, [anytype] * len(tp.defn.type_vars))

    for member in tp.protocol_members:
        typ = get_proper_type(find_member(member, instance, instance))
        if not isinstance(typ, (Overloaded, CallableType)):
            result.append(member)
    return result


def is_callable_compatible(
    left: CallableType,
    right: CallableType,
    *,
    is_compat: Callable[[Type, Type], bool],
    is_compat_return: Optional[Callable[[Type, Type], bool]] = None,
    ignore_return: bool = False,
    ignore_pos_arg_names: bool = False,
    check_args_covariantly: bool = False,
    allow_partial_overlap: bool = False,
    strict_concatenate: bool = False,
) -> bool:
    """Is the left compatible with the right, using the provided compatibility check?

    is_compat:
        The check we want to run against the parameters.

    is_compat_return:
        The check we want to run against the return type.
        If None, use the 'is_compat' check.

    check_args_covariantly:
        If true, check if the left's args is compatible with the right's
        instead of the other way around (contravariantly).

        This function is mostly used to check if the left is a subtype of the right which
        is why the default is to check the args contravariantly. However, it's occasionally
        useful to check the args using some other check, so we leave the variance
        configurable.

        For example, when checking the validity of overloads, it's useful to see if
        the first overload alternative has more precise arguments then the second.
        We would want to check the arguments covariantly in that case.

        Note! The following two function calls are NOT equivalent:

            is_callable_compatible(f, g, is_compat=is_subtype, check_args_covariantly=False)
            is_callable_compatible(g, f, is_compat=is_subtype, check_args_covariantly=True)

        The two calls are similar in that they both check the function arguments in
        the same direction: they both run `is_subtype(argument_from_g, argument_from_f)`.

        However, the two calls differ in which direction they check things like
        keyword arguments. For example, suppose f and g are defined like so:

            def f(x: int, *y: int) -> int: ...
            def g(x: int) -> int: ...

        In this case, the first call will succeed and the second will fail: f is a
        valid stand-in for g but not vice-versa.

    allow_partial_overlap:
        By default this function returns True if and only if *all* calls to left are
        also calls to right (with respect to the provided 'is_compat' function).

        If this parameter is set to 'True', we return True if *there exists at least one*
        call to left that's also a call to right.

        In other words, we perform an existential check instead of a universal one;
        we require left to only overlap with right instead of being a subset.

        For example, suppose we set 'is_compat' to some subtype check and compare following:

            f(x: float, y: str = "...", *args: bool) -> str
            g(*args: int) -> str

        This function would normally return 'False': f is not a subtype of g.
        However, we would return True if this parameter is set to 'True': the two
        calls are compatible if the user runs "f_or_g(3)". In the context of that
        specific call, the two functions effectively have signatures of:

            f2(float) -> str
            g2(int) -> str

        Here, f2 is a valid subtype of g2 so we return True.

        Specifically, if this parameter is set this function will:

        -   Ignore optional arguments on either the left or right that have no
            corresponding match.
        -   No longer mandate optional arguments on either side are also optional
            on the other.
        -   No longer mandate that if right has a *arg or **kwarg that left must also
            have the same.

        Note: when this argument is set to True, this function becomes "symmetric" --
        the following calls are equivalent:

            is_callable_compatible(f, g,
                                   is_compat=some_check,
                                   check_args_covariantly=False,
                                   allow_partial_overlap=True)
            is_callable_compatible(g, f,
                                   is_compat=some_check,
                                   check_args_covariantly=True,
                                   allow_partial_overlap=True)

        If the 'some_check' function is also symmetric, the two calls would be equivalent
        whether or not we check the args covariantly.
    """
    if is_compat_return is None:
        is_compat_return = is_compat

    # If either function is implicitly typed, ignore positional arg names too
    if left.implicit or right.implicit:
        ignore_pos_arg_names = True

    # Non-type cannot be a subtype of type.
    if right.is_type_obj() and not left.is_type_obj():
        return False

    # A callable L is a subtype of a generic callable R if L is a
    # subtype of every type obtained from R by substituting types for
    # the variables of R. We can check this by simply leaving the
    # generic variables of R as type variables, effectively varying
    # over all possible values.

    # It's okay even if these variables share ids with generic
    # type variables of L, because generating and solving
    # constraints for the variables of L to make L a subtype of R
    # (below) treats type variables on the two sides as independent.
    if left.variables:
        # Apply generic type variables away in left via type inference.
        unified = unify_generic_callable(left, right, ignore_return=ignore_return)
        if unified is None:
            return False
        else:
            left = unified

    # If we allow partial overlaps, we don't need to leave R generic:
    # if we can find even just a single typevar assignment which
    # would make these callables compatible, we should return True.

    # So, we repeat the above checks in the opposite direction. This also
    # lets us preserve the 'symmetry' property of allow_partial_overlap.
    if allow_partial_overlap and right.variables:
        unified = unify_generic_callable(right, left, ignore_return=ignore_return)
        if unified is not None:
            right = unified

    # Check return types.
    if not ignore_return and not is_compat_return(left.ret_type, right.ret_type):
        return False

    if check_args_covariantly:
        is_compat = flip_compat_check(is_compat)

    if not strict_concatenate and (left.from_concatenate or right.from_concatenate):
        strict_concatenate_check = False
    else:
        strict_concatenate_check = True

    return are_parameters_compatible(
        left,
        right,
        is_compat=is_compat,
        ignore_pos_arg_names=ignore_pos_arg_names,
        check_args_covariantly=check_args_covariantly,
        allow_partial_overlap=allow_partial_overlap,
        strict_concatenate_check=strict_concatenate_check,
    )


def are_parameters_compatible(
    left: Union[Parameters, CallableType],
    right: Union[Parameters, CallableType],
    *,
    is_compat: Callable[[Type, Type], bool],
    ignore_pos_arg_names: bool = False,
    check_args_covariantly: bool = False,
    allow_partial_overlap: bool = False,
    strict_concatenate_check: bool = True,
) -> bool:
    """Helper function for is_callable_compatible, used for Parameter compatibility"""
    if right.is_ellipsis_args:
        return True

    left_star = left.var_arg()
    left_star2 = left.kw_arg()
    right_star = right.var_arg()
    right_star2 = right.kw_arg()

    # Match up corresponding arguments and check them for compatibility. In
    # every pair (argL, argR) of corresponding arguments from L and R, argL must
    # be "more general" than argR if L is to be a subtype of R.

    # Arguments are corresponding if they either share a name, share a position,
    # or both. If L's corresponding argument is ambiguous, L is not a subtype of R.

    # If left has one corresponding argument by name and another by position,
    # consider them to be one "merged" argument (and not ambiguous) if they're
    # both optional, they're name-only and position-only respectively, and they
    # have the same type.  This rule allows functions with (*args, **kwargs) to
    # properly stand in for the full domain of formal arguments that they're
    # used for in practice.

    # Every argument in R must have a corresponding argument in L, and every
    # required argument in L must have a corresponding argument in R.

    # Phase 1: Confirm every argument in R has a corresponding argument in L.

    # Phase 1a: If left and right can both accept an infinite number of args,
    #           their types must be compatible.
    #
    #           Furthermore, if we're checking for compatibility in all cases,
    #           we confirm that if R accepts an infinite number of arguments,
    #           L must accept the same.
    def _incompatible(
        left_arg: Optional[FormalArgument], right_arg: Optional[FormalArgument]
    ) -> bool:
        if right_arg is None:
            return False
        if left_arg is None:
            return not allow_partial_overlap
        return not is_compat(right_arg.typ, left_arg.typ)

    if _incompatible(left_star, right_star) or _incompatible(left_star2, right_star2):
        return False

    # Phase 1b: Check non-star args: for every arg right can accept, left must
    #           also accept. The only exception is if we are allowing partial
    #           partial overlaps: in that case, we ignore optional args on the right.
    for right_arg in right.formal_arguments():
        left_arg = mypy.typeops.callable_corresponding_argument(left, right_arg)
        if left_arg is None:
            if allow_partial_overlap and not right_arg.required:
                continue
            return False
        if not are_args_compatible(
            left_arg, right_arg, ignore_pos_arg_names, allow_partial_overlap, is_compat
        ):
            return False

    # Phase 1c: Check var args. Right has an infinite series of optional positional
    #           arguments. Get all further positional args of left, and make sure
    #           they're more general then the corresponding member in right.
    if right_star is not None:
        # Synthesize an anonymous formal argument for the right
        right_by_position = right.try_synthesizing_arg_from_vararg(None)
        assert right_by_position is not None

        i = right_star.pos
        assert i is not None
        while i < len(left.arg_kinds) and left.arg_kinds[i].is_positional():
            if allow_partial_overlap and left.arg_kinds[i].is_optional():
                break

            left_by_position = left.argument_by_position(i)
            assert left_by_position is not None

            if not are_args_compatible(
                left_by_position,
                right_by_position,
                ignore_pos_arg_names,
                allow_partial_overlap,
                is_compat,
            ):
                return False
            i += 1

    # Phase 1d: Check kw args. Right has an infinite series of optional named
    #           arguments. Get all further named args of left, and make sure
    #           they're more general then the corresponding member in right.
    if right_star2 is not None:
        right_names = {name for name in right.arg_names if name is not None}
        left_only_names = set()
        for name, kind in zip(left.arg_names, left.arg_kinds):
            if (
                name is None
                or kind.is_star()
                or name in right_names
                or not strict_concatenate_check
            ):
                continue
            left_only_names.add(name)

        # Synthesize an anonymous formal argument for the right
        right_by_name = right.try_synthesizing_arg_from_kwarg(None)
        assert right_by_name is not None

        for name in left_only_names:
            left_by_name = left.argument_by_name(name)
            assert left_by_name is not None

            if allow_partial_overlap and not left_by_name.required:
                continue

            if not are_args_compatible(
                left_by_name, right_by_name, ignore_pos_arg_names, allow_partial_overlap, is_compat
            ):
                return False

    # Phase 2: Left must not impose additional restrictions.
    #          (Every required argument in L must have a corresponding argument in R)
    #          Note: we already checked the *arg and **kwarg arguments in phase 1a.
    for left_arg in left.formal_arguments():
        right_by_name = (
            right.argument_by_name(left_arg.name) if left_arg.name is not None else None
        )

        right_by_pos = (
            right.argument_by_position(left_arg.pos) if left_arg.pos is not None else None
        )

        # If the left hand argument corresponds to two right-hand arguments,
        # neither of them can be required.
        if (
            right_by_name is not None
            and right_by_pos is not None
            and right_by_name != right_by_pos
            and (right_by_pos.required or right_by_name.required)
            and strict_concatenate_check
        ):
            return False

        # All *required* left-hand arguments must have a corresponding
        # right-hand argument.  Optional args do not matter.
        if left_arg.required and right_by_pos is None and right_by_name is None:
            return False

    return True


def are_args_compatible(
    left: FormalArgument,
    right: FormalArgument,
    ignore_pos_arg_names: bool,
    allow_partial_overlap: bool,
    is_compat: Callable[[Type, Type], bool],
) -> bool:
    def is_different(left_item: Optional[object], right_item: Optional[object]) -> bool:
        """Checks if the left and right items are different.

        If the right item is unspecified (e.g. if the right callable doesn't care
        about what name or position its arg has), we default to returning False.

        If we're allowing partial overlap, we also default to returning False
        if the left callable also doesn't care."""
        if right_item is None:
            return False
        if allow_partial_overlap and left_item is None:
            return False
        return left_item != right_item

    # If right has a specific name it wants this argument to be, left must
    # have the same.
    if is_different(left.name, right.name):
        # But pay attention to whether we're ignoring positional arg names
        if not ignore_pos_arg_names or right.pos is None:
            return False

    # If right is at a specific position, left must have the same:
    if is_different(left.pos, right.pos):
        return False

    # If right's argument is optional, left's must also be
    # (unless we're relaxing the checks to allow potential
    # rather then definite compatibility).
    if not allow_partial_overlap and not right.required and left.required:
        return False

    # If we're allowing partial overlaps and neither arg is required,
    # the types don't actually need to be the same
    if allow_partial_overlap and not left.required and not right.required:
        return True

    # Left must have a more general type
    return is_compat(right.typ, left.typ)


def flip_compat_check(is_compat: Callable[[Type, Type], bool]) -> Callable[[Type, Type], bool]:
    def new_is_compat(left: Type, right: Type) -> bool:
        return is_compat(right, left)

    return new_is_compat


def unify_generic_callable(
    type: CallableType,
    target: CallableType,
    ignore_return: bool,
    return_constraint_direction: Optional[int] = None,
) -> Optional[CallableType]:
    """Try to unify a generic callable type with another callable type.

    Return unified CallableType if successful; otherwise, return None.
    """
    import mypy.solve

    if return_constraint_direction is None:
        return_constraint_direction = mypy.constraints.SUBTYPE_OF

    constraints: List[mypy.constraints.Constraint] = []
    for arg_type, target_arg_type in zip(type.arg_types, target.arg_types):
        c = mypy.constraints.infer_constraints(
            arg_type, target_arg_type, mypy.constraints.SUPERTYPE_OF
        )
        constraints.extend(c)
    if not ignore_return:
        c = mypy.constraints.infer_constraints(
            type.ret_type, target.ret_type, return_constraint_direction
        )
        constraints.extend(c)
    type_var_ids = [tvar.id for tvar in type.variables]
    inferred_vars = mypy.solve.solve_constraints(type_var_ids, constraints)
    if None in inferred_vars:
        return None
    non_none_inferred_vars = cast(List[Type], inferred_vars)
    had_errors = False

    def report(*args: Any) -> None:
        nonlocal had_errors
        had_errors = True

    applied = mypy.applytype.apply_generic_arguments(
        type, non_none_inferred_vars, report, context=target
    )
    if had_errors:
        return None
    return applied


def try_restrict_literal_union(t: UnionType, s: Type) -> Optional[List[Type]]:
    """Return the items of t, excluding any occurrence of s, if and only if
      - t only contains simple literals
      - s is a simple literal

    Otherwise, returns None
    """
    ps = get_proper_type(s)
    if not mypy.typeops.is_simple_literal(ps):
        return None

    new_items: List[Type] = []
    for i in t.relevant_items():
        pi = get_proper_type(i)
        if not mypy.typeops.is_simple_literal(pi):
            return None
        if pi != ps:
            new_items.append(i)
    return new_items


def restrict_subtype_away(t: Type, s: Type, *, ignore_promotions: bool = False) -> Type:
    """Return t minus s for runtime type assertions.

    If we can't determine a precise result, return a supertype of the
    ideal result (just t is a valid result).

    This is used for type inference of runtime type checks such as
    isinstance(). Currently this just removes elements of a union type.
    """
    p_t = get_proper_type(t)
    if isinstance(p_t, UnionType):
        new_items = try_restrict_literal_union(p_t, s)
        if new_items is None:
            new_items = [
                restrict_subtype_away(item, s, ignore_promotions=ignore_promotions)
                for item in p_t.relevant_items()
                if (
                    isinstance(get_proper_type(item), AnyType)
                    or not covers_at_runtime(item, s, ignore_promotions)
                )
            ]
        return UnionType.make_union(new_items)
    elif covers_at_runtime(t, s, ignore_promotions):
        return UninhabitedType()
    else:
        return t


def covers_at_runtime(item: Type, supertype: Type, ignore_promotions: bool) -> bool:
    """Will isinstance(item, supertype) always return True at runtime?"""
    item = get_proper_type(item)
    supertype = get_proper_type(supertype)

    # Since runtime type checks will ignore type arguments, erase the types.
    supertype = erase_type(supertype)
    if is_proper_subtype(
        erase_type(item), supertype, ignore_promotions=ignore_promotions, erase_instances=True
    ):
        return True
    if isinstance(supertype, Instance) and supertype.type.is_protocol:
        # TODO: Implement more robust support for runtime isinstance() checks, see issue #3827.
        if is_proper_subtype(item, supertype, ignore_promotions=ignore_promotions):
            return True
    if isinstance(item, TypedDictType) and isinstance(supertype, Instance):
        # Special case useful for selecting TypedDicts from unions using isinstance(x, dict).
        if supertype.type.fullname == "builtins.dict":
            return True
    # TODO: Add more special cases.
    return False


def is_more_precise(left: Type, right: Type, *, ignore_promotions: bool = False) -> bool:
    """Check if left is a more precise type than right.

    A left is a proper subtype of right, left is also more precise than
    right. Also, if right is Any, left is more precise than right, for
    any left.
    """
    # TODO Should List[int] be more precise than List[Any]?
    right = get_proper_type(right)
    if isinstance(right, AnyType):
        return True
    return is_proper_subtype(left, right, ignore_promotions=ignore_promotions)
