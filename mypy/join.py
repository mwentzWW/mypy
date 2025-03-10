"""Calculation of the least upper bound types (joins)."""

from typing import List, Optional, Tuple

import mypy.typeops
from mypy.maptype import map_instance_to_supertype
from mypy.nodes import CONTRAVARIANT, COVARIANT, INVARIANT
from mypy.state import state
from mypy.subtypes import (
    find_member,
    is_equivalent,
    is_proper_subtype,
    is_protocol_implementation,
    is_subtype,
)
from mypy.types import (
    AnyType,
    CallableType,
    DeletedType,
    ErasedType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecType,
    PartialType,
    PlaceholderType,
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
    get_proper_types,
)


class InstanceJoiner:
    def __init__(self) -> None:
        self.seen_instances: List[Tuple[Instance, Instance]] = []

    def join_instances(self, t: Instance, s: Instance) -> ProperType:
        if (t, s) in self.seen_instances or (s, t) in self.seen_instances:
            return object_from_instance(t)

        self.seen_instances.append((t, s))

        # Calculate the join of two instance types
        if t.type == s.type:
            # Simplest case: join two types with the same base type (but
            # potentially different arguments).

            # Combine type arguments.
            args: List[Type] = []
            # N.B: We use zip instead of indexing because the lengths might have
            # mismatches during daemon reprocessing.
            for ta, sa, type_var in zip(t.args, s.args, t.type.defn.type_vars):
                ta_proper = get_proper_type(ta)
                sa_proper = get_proper_type(sa)
                new_type: Optional[Type] = None
                if isinstance(ta_proper, AnyType):
                    new_type = AnyType(TypeOfAny.from_another_any, ta_proper)
                elif isinstance(sa_proper, AnyType):
                    new_type = AnyType(TypeOfAny.from_another_any, sa_proper)
                elif isinstance(type_var, TypeVarType):
                    if type_var.variance == COVARIANT:
                        new_type = join_types(ta, sa, self)
                        if len(type_var.values) != 0 and new_type not in type_var.values:
                            self.seen_instances.pop()
                            return object_from_instance(t)
                        if not is_subtype(new_type, type_var.upper_bound):
                            self.seen_instances.pop()
                            return object_from_instance(t)
                    # TODO: contravariant case should use meet but pass seen instances as
                    # an argument to keep track of recursive checks.
                    elif type_var.variance in (INVARIANT, CONTRAVARIANT):
                        if not is_equivalent(ta, sa):
                            self.seen_instances.pop()
                            return object_from_instance(t)
                        # If the types are different but equivalent, then an Any is involved
                        # so using a join in the contravariant case is also OK.
                        new_type = join_types(ta, sa, self)
                else:
                    # ParamSpec type variables behave the same, independent of variance
                    if not is_equivalent(ta, sa):
                        return get_proper_type(type_var.upper_bound)
                    new_type = join_types(ta, sa, self)
                assert new_type is not None
                args.append(new_type)
            result: ProperType = Instance(t.type, args)
        elif t.type.bases and is_subtype(t, s, ignore_type_params=True):
            result = self.join_instances_via_supertype(t, s)
        else:
            # Now t is not a subtype of s, and t != s. Now s could be a subtype
            # of t; alternatively, we need to find a common supertype. This works
            # in of the both cases.
            result = self.join_instances_via_supertype(s, t)

        self.seen_instances.pop()
        return result

    def join_instances_via_supertype(self, t: Instance, s: Instance) -> ProperType:
        # Give preference to joins via duck typing relationship, so that
        # join(int, float) == float, for example.
        for p in t.type._promote:
            if is_subtype(p, s):
                return join_types(p, s, self)
        for p in s.type._promote:
            if is_subtype(p, t):
                return join_types(t, p, self)

        # Compute the "best" supertype of t when joined with s.
        # The definition of "best" may evolve; for now it is the one with
        # the longest MRO.  Ties are broken by using the earlier base.
        best: Optional[ProperType] = None
        for base in t.type.bases:
            mapped = map_instance_to_supertype(t, base.type)
            res = self.join_instances(mapped, s)
            if best is None or is_better(res, best):
                best = res
        assert best is not None
        for promote in t.type._promote:
            promote = get_proper_type(promote)
            if isinstance(promote, Instance):
                res = self.join_instances(promote, s)
                if is_better(res, best):
                    best = res
        return best


def join_simple(declaration: Optional[Type], s: Type, t: Type) -> ProperType:
    """Return a simple least upper bound given the declared type."""
    # TODO: check infinite recursion for aliases here.
    declaration = get_proper_type(declaration)
    s = get_proper_type(s)
    t = get_proper_type(t)

    if (s.can_be_true, s.can_be_false) != (t.can_be_true, t.can_be_false):
        # if types are restricted in different ways, use the more general versions
        s = mypy.typeops.true_or_false(s)
        t = mypy.typeops.true_or_false(t)

    if isinstance(s, AnyType):
        return s

    if isinstance(s, ErasedType):
        return t

    if is_proper_subtype(s, t):
        return t

    if is_proper_subtype(t, s):
        return s

    if isinstance(declaration, UnionType):
        return mypy.typeops.make_simplified_union([s, t])

    if isinstance(s, NoneType) and not isinstance(t, NoneType):
        s, t = t, s

    if isinstance(s, UninhabitedType) and not isinstance(t, UninhabitedType):
        s, t = t, s

    value = t.accept(TypeJoinVisitor(s))
    if declaration is None or is_subtype(value, declaration):
        return value

    return declaration


def trivial_join(s: Type, t: Type) -> ProperType:
    """Return one of types (expanded) if it is a supertype of other, otherwise top type."""
    if is_subtype(s, t):
        return get_proper_type(t)
    elif is_subtype(t, s):
        return get_proper_type(s)
    else:
        return object_or_any_from_type(get_proper_type(t))


def join_types(s: Type, t: Type, instance_joiner: Optional[InstanceJoiner] = None) -> ProperType:
    """Return the least upper bound of s and t.

    For example, the join of 'int' and 'object' is 'object'.
    """
    if mypy.typeops.is_recursive_pair(s, t):
        # This case can trigger an infinite recursion, general support for this will be
        # tricky so we use a trivial join (like for protocols).
        return trivial_join(s, t)
    s = get_proper_type(s)
    t = get_proper_type(t)

    if (s.can_be_true, s.can_be_false) != (t.can_be_true, t.can_be_false):
        # if types are restricted in different ways, use the more general versions
        s = mypy.typeops.true_or_false(s)
        t = mypy.typeops.true_or_false(t)

    if isinstance(s, UnionType) and not isinstance(t, UnionType):
        s, t = t, s

    if isinstance(s, AnyType):
        return s

    if isinstance(s, ErasedType):
        return t

    if isinstance(s, NoneType) and not isinstance(t, NoneType):
        s, t = t, s

    if isinstance(s, UninhabitedType) and not isinstance(t, UninhabitedType):
        s, t = t, s

    # We shouldn't run into PlaceholderTypes here, but in practice we can encounter them
    # here in the presence of undefined names
    if isinstance(t, PlaceholderType) and not isinstance(s, PlaceholderType):
        # mypyc does not allow switching the values like above.
        return s.accept(TypeJoinVisitor(t))
    elif isinstance(t, PlaceholderType):
        return AnyType(TypeOfAny.from_error)

    # Use a visitor to handle non-trivial cases.
    return t.accept(TypeJoinVisitor(s, instance_joiner))


class TypeJoinVisitor(TypeVisitor[ProperType]):
    """Implementation of the least upper bound algorithm.

    Attributes:
      s: The other (left) type operand.
    """

    def __init__(self, s: ProperType, instance_joiner: Optional[InstanceJoiner] = None) -> None:
        self.s = s
        self.instance_joiner = instance_joiner

    def visit_unbound_type(self, t: UnboundType) -> ProperType:
        return AnyType(TypeOfAny.special_form)

    def visit_union_type(self, t: UnionType) -> ProperType:
        if is_proper_subtype(self.s, t):
            return t
        else:
            return mypy.typeops.make_simplified_union([self.s, t])

    def visit_any(self, t: AnyType) -> ProperType:
        return t

    def visit_none_type(self, t: NoneType) -> ProperType:
        if state.strict_optional:
            if isinstance(self.s, (NoneType, UninhabitedType)):
                return t
            elif isinstance(self.s, UnboundType):
                return AnyType(TypeOfAny.special_form)
            else:
                return mypy.typeops.make_simplified_union([self.s, t])
        else:
            return self.s

    def visit_uninhabited_type(self, t: UninhabitedType) -> ProperType:
        return self.s

    def visit_deleted_type(self, t: DeletedType) -> ProperType:
        return self.s

    def visit_erased_type(self, t: ErasedType) -> ProperType:
        return self.s

    def visit_type_var(self, t: TypeVarType) -> ProperType:
        if isinstance(self.s, TypeVarType) and self.s.id == t.id:
            return self.s
        else:
            return self.default(self.s)

    def visit_param_spec(self, t: ParamSpecType) -> ProperType:
        if self.s == t:
            return t
        return self.default(self.s)

    def visit_type_var_tuple(self, t: TypeVarTupleType) -> ProperType:
        if self.s == t:
            return t
        return self.default(self.s)

    def visit_unpack_type(self, t: UnpackType) -> UnpackType:
        raise NotImplementedError

    def visit_parameters(self, t: Parameters) -> ProperType:
        if self.s == t:
            return t
        else:
            return self.default(self.s)

    def visit_instance(self, t: Instance) -> ProperType:
        if isinstance(self.s, Instance):
            if self.instance_joiner is None:
                self.instance_joiner = InstanceJoiner()
            nominal = self.instance_joiner.join_instances(t, self.s)
            structural: Optional[Instance] = None
            if t.type.is_protocol and is_protocol_implementation(self.s, t):
                structural = t
            elif self.s.type.is_protocol and is_protocol_implementation(t, self.s):
                structural = self.s
            # Structural join is preferred in the case where we have found both
            # structural and nominal and they have same MRO length (see two comments
            # in join_instances_via_supertype). Otherwise, just return the nominal join.
            if not structural or is_better(nominal, structural):
                return nominal
            return structural
        elif isinstance(self.s, FunctionLike):
            if t.type.is_protocol:
                call = unpack_callback_protocol(t)
                if call:
                    return join_types(call, self.s)
            return join_types(t, self.s.fallback)
        elif isinstance(self.s, TypeType):
            return join_types(t, self.s)
        elif isinstance(self.s, TypedDictType):
            return join_types(t, self.s)
        elif isinstance(self.s, TupleType):
            return join_types(t, self.s)
        elif isinstance(self.s, LiteralType):
            return join_types(t, self.s)
        else:
            return self.default(self.s)

    def visit_callable_type(self, t: CallableType) -> ProperType:
        if isinstance(self.s, CallableType) and is_similar_callables(t, self.s):
            if is_equivalent(t, self.s):
                return combine_similar_callables(t, self.s)
            result = join_similar_callables(t, self.s)
            # We set the from_type_type flag to suppress error when a collection of
            # concrete class objects gets inferred as their common abstract superclass.
            if not (
                (t.is_type_obj() and t.type_object().is_abstract)
                or (self.s.is_type_obj() and self.s.type_object().is_abstract)
            ):
                result.from_type_type = True
            if any(
                isinstance(tp, (NoneType, UninhabitedType))
                for tp in get_proper_types(result.arg_types)
            ):
                # We don't want to return unusable Callable, attempt fallback instead.
                return join_types(t.fallback, self.s)
            return result
        elif isinstance(self.s, Overloaded):
            # Switch the order of arguments to that we'll get to visit_overloaded.
            return join_types(t, self.s)
        elif isinstance(self.s, Instance) and self.s.type.is_protocol:
            call = unpack_callback_protocol(self.s)
            if call:
                return join_types(t, call)
        return join_types(t.fallback, self.s)

    def visit_overloaded(self, t: Overloaded) -> ProperType:
        # This is more complex than most other cases. Here are some
        # examples that illustrate how this works.
        #
        # First let's define a concise notation:
        #  - Cn are callable types (for n in 1, 2, ...)
        #  - Ov(C1, C2, ...) is an overloaded type with items C1, C2, ...
        #  - Callable[[T, ...], S] is written as [T, ...] -> S.
        #
        # We want some basic properties to hold (assume Cn are all
        # unrelated via Any-similarity):
        #
        #   join(Ov(C1, C2), C1) == C1
        #   join(Ov(C1, C2), Ov(C1, C2)) == Ov(C1, C2)
        #   join(Ov(C1, C2), Ov(C1, C3)) == C1
        #   join(Ov(C2, C2), C3) == join of fallback types
        #
        # The presence of Any types makes things more interesting. The join is the
        # most general type we can get with respect to Any:
        #
        #   join(Ov([int] -> int, [str] -> str), [Any] -> str) == Any -> str
        #
        # We could use a simplification step that removes redundancies, but that's not
        # implemented right now. Consider this example, where we get a redundancy:
        #
        #   join(Ov([int, Any] -> Any, [str, Any] -> Any), [Any, int] -> Any) ==
        #       Ov([Any, int] -> Any, [Any, int] -> Any)
        #
        # TODO: Consider more cases of callable subtyping.
        result: List[CallableType] = []
        s = self.s
        if isinstance(s, FunctionLike):
            # The interesting case where both types are function types.
            for t_item in t.items:
                for s_item in s.items:
                    if is_similar_callables(t_item, s_item):
                        if is_equivalent(t_item, s_item):
                            result.append(combine_similar_callables(t_item, s_item))
                        elif is_subtype(t_item, s_item):
                            result.append(s_item)
            if result:
                # TODO: Simplify redundancies from the result.
                if len(result) == 1:
                    return result[0]
                else:
                    return Overloaded(result)
            return join_types(t.fallback, s.fallback)
        elif isinstance(s, Instance) and s.type.is_protocol:
            call = unpack_callback_protocol(s)
            if call:
                return join_types(t, call)
        return join_types(t.fallback, s)

    def visit_tuple_type(self, t: TupleType) -> ProperType:
        # When given two fixed-length tuples:
        # * If they have the same length, join their subtypes item-wise:
        #   Tuple[int, bool] + Tuple[bool, bool] becomes Tuple[int, bool]
        # * If lengths do not match, return a variadic tuple:
        #   Tuple[bool, int] + Tuple[bool] becomes Tuple[int, ...]
        #
        # Otherwise, `t` is a fixed-length tuple but `self.s` is NOT:
        # * Joining with a variadic tuple returns variadic tuple:
        #   Tuple[int, bool] + Tuple[bool, ...] becomes Tuple[int, ...]
        # * Joining with any Sequence also returns a Sequence:
        #   Tuple[int, bool] + List[bool] becomes Sequence[int]
        if isinstance(self.s, TupleType) and self.s.length() == t.length():
            if self.instance_joiner is None:
                self.instance_joiner = InstanceJoiner()
            fallback = self.instance_joiner.join_instances(
                mypy.typeops.tuple_fallback(self.s), mypy.typeops.tuple_fallback(t)
            )
            assert isinstance(fallback, Instance)
            if self.s.length() == t.length():
                items: List[Type] = []
                for i in range(t.length()):
                    items.append(self.join(t.items[i], self.s.items[i]))
                return TupleType(items, fallback)
            else:
                return fallback
        else:
            return join_types(self.s, mypy.typeops.tuple_fallback(t))

    def visit_typeddict_type(self, t: TypedDictType) -> ProperType:
        if isinstance(self.s, TypedDictType):
            items = {
                item_name: s_item_type
                for (item_name, s_item_type, t_item_type) in self.s.zip(t)
                if (
                    is_equivalent(s_item_type, t_item_type)
                    and (item_name in t.required_keys) == (item_name in self.s.required_keys)
                )
            }
            fallback = self.s.create_anonymous_fallback()
            # We need to filter by items.keys() since some required keys present in both t and
            # self.s might be missing from the join if the types are incompatible.
            required_keys = set(items.keys()) & t.required_keys & self.s.required_keys
            return TypedDictType(items, required_keys, fallback)
        elif isinstance(self.s, Instance):
            return join_types(self.s, t.fallback)
        else:
            return self.default(self.s)

    def visit_literal_type(self, t: LiteralType) -> ProperType:
        if isinstance(self.s, LiteralType):
            if t == self.s:
                return t
            if self.s.fallback.type.is_enum and t.fallback.type.is_enum:
                return mypy.typeops.make_simplified_union([self.s, t])
            return join_types(self.s.fallback, t.fallback)
        else:
            return join_types(self.s, t.fallback)

    def visit_partial_type(self, t: PartialType) -> ProperType:
        # We only have partial information so we can't decide the join result. We should
        # never get here.
        assert False, "Internal error"

    def visit_type_type(self, t: TypeType) -> ProperType:
        if isinstance(self.s, TypeType):
            return TypeType.make_normalized(self.join(t.item, self.s.item), line=t.line)
        elif isinstance(self.s, Instance) and self.s.type.fullname == "builtins.type":
            return self.s
        else:
            return self.default(self.s)

    def visit_type_alias_type(self, t: TypeAliasType) -> ProperType:
        assert False, f"This should be never called, got {t}"

    def join(self, s: Type, t: Type) -> ProperType:
        return join_types(s, t)

    def default(self, typ: Type) -> ProperType:
        typ = get_proper_type(typ)
        if isinstance(typ, Instance):
            return object_from_instance(typ)
        elif isinstance(typ, UnboundType):
            return AnyType(TypeOfAny.special_form)
        elif isinstance(typ, TupleType):
            return self.default(mypy.typeops.tuple_fallback(typ))
        elif isinstance(typ, TypedDictType):
            return self.default(typ.fallback)
        elif isinstance(typ, FunctionLike):
            return self.default(typ.fallback)
        elif isinstance(typ, TypeVarType):
            return self.default(typ.upper_bound)
        elif isinstance(typ, ParamSpecType):
            return self.default(typ.upper_bound)
        else:
            return AnyType(TypeOfAny.special_form)


def is_better(t: Type, s: Type) -> bool:
    # Given two possible results from join_instances_via_supertype(),
    # indicate whether t is the better one.
    t = get_proper_type(t)
    s = get_proper_type(s)

    if isinstance(t, Instance):
        if not isinstance(s, Instance):
            return True
        # Use len(mro) as a proxy for the better choice.
        if len(t.type.mro) > len(s.type.mro):
            return True
    return False


def is_similar_callables(t: CallableType, s: CallableType) -> bool:
    """Return True if t and s have identical numbers of
    arguments, default arguments and varargs.
    """
    return (
        len(t.arg_types) == len(s.arg_types)
        and t.min_args == s.min_args
        and t.is_var_arg == s.is_var_arg
    )


def join_similar_callables(t: CallableType, s: CallableType) -> CallableType:
    from mypy.meet import meet_types

    arg_types: List[Type] = []
    for i in range(len(t.arg_types)):
        arg_types.append(meet_types(t.arg_types[i], s.arg_types[i]))
    # TODO in combine_similar_callables also applies here (names and kinds)
    # The fallback type can be either 'function' or 'type'. The result should have 'type' as
    # fallback only if both operands have it as 'type'.
    if t.fallback.type.fullname != "builtins.type":
        fallback = t.fallback
    else:
        fallback = s.fallback
    return t.copy_modified(
        arg_types=arg_types,
        arg_names=combine_arg_names(t, s),
        ret_type=join_types(t.ret_type, s.ret_type),
        fallback=fallback,
        name=None,
    )


def combine_similar_callables(t: CallableType, s: CallableType) -> CallableType:
    arg_types: List[Type] = []
    for i in range(len(t.arg_types)):
        arg_types.append(join_types(t.arg_types[i], s.arg_types[i]))
    # TODO kinds and argument names
    # The fallback type can be either 'function' or 'type'. The result should have 'type' as
    # fallback only if both operands have it as 'type'.
    if t.fallback.type.fullname != "builtins.type":
        fallback = t.fallback
    else:
        fallback = s.fallback
    return t.copy_modified(
        arg_types=arg_types,
        arg_names=combine_arg_names(t, s),
        ret_type=join_types(t.ret_type, s.ret_type),
        fallback=fallback,
        name=None,
    )


def combine_arg_names(t: CallableType, s: CallableType) -> List[Optional[str]]:
    """Produces a list of argument names compatible with both callables.

    For example, suppose 't' and 's' have the following signatures:

    - t: (a: int, b: str, X: str) -> None
    - s: (a: int, b: str, Y: str) -> None

    This function would return ["a", "b", None]. This information
    is then used above to compute the join of t and s, which results
    in a signature of (a: int, b: str, str) -> None.

    Note that the third argument's name is omitted and 't' and 's'
    are both valid subtypes of this inferred signature.

    Precondition: is_similar_types(t, s) is true.
    """
    num_args = len(t.arg_types)
    new_names = []
    for i in range(num_args):
        t_name = t.arg_names[i]
        s_name = s.arg_names[i]
        if t_name == s_name or t.arg_kinds[i].is_named() or s.arg_kinds[i].is_named():
            new_names.append(t_name)
        else:
            new_names.append(None)
    return new_names


def object_from_instance(instance: Instance) -> Instance:
    """Construct the type 'builtins.object' from an instance type."""
    # Use the fact that 'object' is always the last class in the mro.
    res = Instance(instance.type.mro[-1], [])
    return res


def object_or_any_from_type(typ: ProperType) -> ProperType:
    # Similar to object_from_instance() but tries hard for all types.
    # TODO: find a better way to get object, or make this more reliable.
    if isinstance(typ, Instance):
        return object_from_instance(typ)
    elif isinstance(typ, (CallableType, TypedDictType, LiteralType)):
        return object_from_instance(typ.fallback)
    elif isinstance(typ, TupleType):
        return object_from_instance(typ.partial_fallback)
    elif isinstance(typ, TypeType):
        return object_or_any_from_type(typ.item)
    elif isinstance(typ, TypeVarType) and isinstance(typ.upper_bound, ProperType):
        return object_or_any_from_type(typ.upper_bound)
    elif isinstance(typ, UnionType):
        for item in typ.items:
            if isinstance(item, ProperType):
                candidate = object_or_any_from_type(item)
                if isinstance(candidate, Instance):
                    return candidate
    return AnyType(TypeOfAny.implementation_artifact)


def join_type_list(types: List[Type]) -> ProperType:
    if not types:
        # This is a little arbitrary but reasonable. Any empty tuple should be compatible
        # with all variable length tuples, and this makes it possible.
        return UninhabitedType()
    joined = get_proper_type(types[0])
    for t in types[1:]:
        joined = join_types(joined, t)
    return joined


def unpack_callback_protocol(t: Instance) -> Optional[Type]:
    assert t.type.is_protocol
    if t.type.protocol_members == ["__call__"]:
        return find_member("__call__", t, t, is_operator=True)
    return None
