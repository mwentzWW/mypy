import copy
import re
import sys
import typing  # for typing.Type, which conflicts with types.Type
import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar, Union, cast

from typing_extensions import Final, Literal, overload

from mypy import defaults, errorcodes as codes, message_registry
from mypy.errors import Errors
from mypy.nodes import (
    ARG_NAMED,
    ARG_NAMED_OPT,
    ARG_OPT,
    ARG_POS,
    ARG_STAR,
    ARG_STAR2,
    ArgKind,
    Argument,
    AssertStmt,
    AssignmentExpr,
    AssignmentStmt,
    AwaitExpr,
    Block,
    BreakStmt,
    BytesExpr,
    CallExpr,
    ClassDef,
    ComparisonExpr,
    ComplexExpr,
    ConditionalExpr,
    ContinueStmt,
    Decorator,
    DelStmt,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    Expression,
    ExpressionStmt,
    FakeInfo,
    FloatExpr,
    ForStmt,
    FuncDef,
    GeneratorExpr,
    GlobalDecl,
    IfStmt,
    Import,
    ImportAll,
    ImportBase,
    ImportFrom,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MatchStmt,
    MemberExpr,
    MypyFile,
    NameExpr,
    Node,
    NonlocalDecl,
    OperatorAssignmentStmt,
    OpExpr,
    OverloadedFuncDef,
    OverloadPart,
    PassStmt,
    RaiseStmt,
    RefExpr,
    ReturnStmt,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    Statement,
    StrExpr,
    SuperExpr,
    TempNode,
    TryStmt,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
    YieldExpr,
    YieldFromExpr,
    check_arg_names,
)
from mypy.options import Options
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    SequencePattern,
    SingletonPattern,
    StarredPattern,
    ValuePattern,
)
from mypy.reachability import infer_reachability_of_if_statement, mark_block_unreachable
from mypy.sharedparse import argument_elide_name, special_function_elide_names
from mypy.types import (
    AnyType,
    CallableArgument,
    CallableType,
    EllipsisType,
    Instance,
    ProperType,
    RawExpressionType,
    TupleType,
    Type,
    TypeList,
    TypeOfAny,
    UnboundType,
    UnionType,
)
from mypy.util import bytes_to_human_readable_repr, unnamed_function

try:
    # pull this into a final variable to make mypyc be quiet about the
    # the default argument warning
    PY_MINOR_VERSION: Final = sys.version_info[1]

    # Check if we can use the stdlib ast module instead of typed_ast.
    if sys.version_info >= (3, 8):
        import ast as ast3

        assert (
            "kind" in ast3.Constant._fields
        ), f"This 3.8.0 alpha ({sys.version.split()[0]}) is too old; 3.8.0a3 required"
        # TODO: Num, Str, Bytes, NameConstant, Ellipsis are deprecated in 3.8.
        # TODO: Index, ExtSlice are deprecated in 3.9.
        from ast import (
            AST,
            Attribute,
            Bytes,
            Call,
            Ellipsis as ast3_Ellipsis,
            Expression as ast3_Expression,
            FunctionType,
            Index,
            Name,
            NameConstant,
            Num,
            Starred,
            Str,
            UnaryOp,
            USub,
        )

        def ast3_parse(
            source: Union[str, bytes],
            filename: str,
            mode: str,
            feature_version: int = PY_MINOR_VERSION,
        ) -> AST:
            return ast3.parse(
                source,
                filename,
                mode,
                type_comments=True,  # This works the magic
                feature_version=feature_version,
            )

        NamedExpr = ast3.NamedExpr
        Constant = ast3.Constant
    else:
        from typed_ast import ast3
        from typed_ast.ast3 import (
            AST,
            Attribute,
            Bytes,
            Call,
            Ellipsis as ast3_Ellipsis,
            Expression as ast3_Expression,
            FunctionType,
            Index,
            Name,
            NameConstant,
            Num,
            Starred,
            Str,
            UnaryOp,
            USub,
        )

        def ast3_parse(
            source: Union[str, bytes],
            filename: str,
            mode: str,
            feature_version: int = PY_MINOR_VERSION,
        ) -> AST:
            return ast3.parse(source, filename, mode, feature_version=feature_version)

        # These don't exist before 3.8
        NamedExpr = Any
        Constant = Any

    if sys.version_info >= (3, 10):
        Match = ast3.Match
        MatchValue = ast3.MatchValue
        MatchSingleton = ast3.MatchSingleton
        MatchSequence = ast3.MatchSequence
        MatchStar = ast3.MatchStar
        MatchMapping = ast3.MatchMapping
        MatchClass = ast3.MatchClass
        MatchAs = ast3.MatchAs
        MatchOr = ast3.MatchOr
        AstNode = Union[ast3.expr, ast3.stmt, ast3.pattern, ast3.ExceptHandler]
    else:
        Match = Any
        MatchValue = Any
        MatchSingleton = Any
        MatchSequence = Any
        MatchStar = Any
        MatchMapping = Any
        MatchClass = Any
        MatchAs = Any
        MatchOr = Any
        AstNode = Union[ast3.expr, ast3.stmt, ast3.ExceptHandler]
except ImportError:
    try:
        from typed_ast import ast35  # type: ignore[attr-defined]  # noqa: F401
    except ImportError:
        print(
            "The typed_ast package is not installed.\n"
            "You can install it with `python3 -m pip install typed-ast`.",
            file=sys.stderr,
        )
    else:
        print(
            "You need a more recent version of the typed_ast package.\n"
            "You can update to the latest version with "
            "`python3 -m pip install -U typed-ast`.",
            file=sys.stderr,
        )
    sys.exit(1)

N = TypeVar("N", bound=Node)

# There is no way to create reasonable fallbacks at this stage,
# they must be patched later.
MISSING_FALLBACK: Final = FakeInfo("fallback can't be filled out until semanal")
_dummy_fallback: Final = Instance(MISSING_FALLBACK, [], -1)

TYPE_COMMENT_SYNTAX_ERROR: Final = "syntax error in type comment"

INVALID_TYPE_IGNORE: Final = 'Invalid "type: ignore" comment'

TYPE_IGNORE_PATTERN: Final = re.compile(r"[^#]*#\s*type:\s*ignore\s*(.*)")


def parse(
    source: Union[str, bytes],
    fnam: str,
    module: Optional[str],
    errors: Optional[Errors] = None,
    options: Optional[Options] = None,
) -> MypyFile:

    """Parse a source file, without doing any semantic analysis.

    Return the parse tree. If errors is not provided, raise ParseError
    on failure. Otherwise, use the errors object to report parse errors.
    """
    raise_on_error = False
    if errors is None:
        errors = Errors()
        raise_on_error = True
    if options is None:
        options = Options()
    errors.set_file(fnam, module)
    is_stub_file = fnam.endswith(".pyi")
    if is_stub_file:
        feature_version = defaults.PYTHON3_VERSION[1]
    else:
        assert options.python_version[0] >= 3
        feature_version = options.python_version[1]
    try:
        # Disable deprecation warnings about \u
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            ast = ast3_parse(source, fnam, "exec", feature_version=feature_version)

        tree = ASTConverter(options=options, is_stub=is_stub_file, errors=errors).visit(ast)
        tree.path = fnam
        tree.is_stub = is_stub_file
    except SyntaxError as e:
        # alias to please mypyc
        is_py38_or_earlier = sys.version_info < (3, 9)
        if is_py38_or_earlier and e.filename == "<fstring>":
            # In Python 3.8 and earlier, syntax errors in f-strings have lineno relative to the
            # start of the f-string. This would be misleading, as mypy will report the error as the
            # lineno within the file.
            e.lineno = None
        message = e.msg
        if feature_version > sys.version_info.minor and message.startswith("invalid syntax"):
            python_version_str = f"{options.python_version[0]}.{options.python_version[1]}"
            message += f"; you likely need to run mypy using Python {python_version_str} or newer"
        errors.report(
            e.lineno if e.lineno is not None else -1,
            e.offset,
            message,
            blocker=True,
            code=codes.SYNTAX,
        )
        tree = MypyFile([], [], False, {})

    if raise_on_error and errors.is_errors():
        errors.raise_error()

    return tree


def parse_type_ignore_tag(tag: Optional[str]) -> Optional[List[str]]:
    """Parse optional "[code, ...]" tag after "# type: ignore".

    Return:
     * [] if no tag was found (ignore all errors)
     * list of ignored error codes if a tag was found
     * None if the tag was invalid.
    """
    if not tag or tag.strip() == "" or tag.strip().startswith("#"):
        # No tag -- ignore all errors.
        return []
    m = re.match(r"\s*\[([^]#]*)\]\s*(#.*)?$", tag)
    if m is None:
        # Invalid "# type: ignore" comment.
        return None
    return [code.strip() for code in m.group(1).split(",")]


def parse_type_comment(
    type_comment: str, line: int, column: int, errors: Optional[Errors]
) -> Tuple[Optional[List[str]], Optional[ProperType]]:
    """Parse type portion of a type comment (+ optional type ignore).

    Return (ignore info, parsed type).
    """
    try:
        typ = ast3_parse(type_comment, "<type_comment>", "eval")
    except SyntaxError:
        if errors is not None:
            stripped_type = type_comment.split("#", 2)[0].strip()
            err_msg = f'{TYPE_COMMENT_SYNTAX_ERROR} "{stripped_type}"'
            errors.report(line, column, err_msg, blocker=True, code=codes.SYNTAX)
            return None, None
        else:
            raise
    else:
        extra_ignore = TYPE_IGNORE_PATTERN.match(type_comment)
        if extra_ignore:
            # Typeshed has a non-optional return type for group!
            tag: Optional[str] = cast(Any, extra_ignore).group(1)
            ignored: Optional[List[str]] = parse_type_ignore_tag(tag)
            if ignored is None:
                if errors is not None:
                    errors.report(line, column, INVALID_TYPE_IGNORE, code=codes.SYNTAX)
                else:
                    raise SyntaxError
        else:
            ignored = None
        assert isinstance(typ, ast3_Expression)
        converted = TypeConverter(
            errors, line=line, override_column=column, is_evaluated=False
        ).visit(typ.body)
        return ignored, converted


def parse_type_string(
    expr_string: str, expr_fallback_name: str, line: int, column: int
) -> ProperType:
    """Parses a type that was originally present inside of an explicit string.

    For example, suppose we have the type `Foo["blah"]`. We should parse the
    string expression "blah" using this function.
    """
    try:
        _, node = parse_type_comment(expr_string.strip(), line=line, column=column, errors=None)
        if isinstance(node, UnboundType) and node.original_str_expr is None:
            node.original_str_expr = expr_string
            node.original_str_fallback = expr_fallback_name
            return node
        elif isinstance(node, UnionType):
            return node
        else:
            return RawExpressionType(expr_string, expr_fallback_name, line, column)
    except (SyntaxError, ValueError):
        # Note: the parser will raise a `ValueError` instead of a SyntaxError if
        # the string happens to contain things like \x00.
        return RawExpressionType(expr_string, expr_fallback_name, line, column)


def is_no_type_check_decorator(expr: ast3.expr) -> bool:
    if isinstance(expr, Name):
        return expr.id == "no_type_check"
    elif isinstance(expr, Attribute):
        if isinstance(expr.value, Name):
            return expr.value.id == "typing" and expr.attr == "no_type_check"
    return False


class ASTConverter:
    def __init__(self, options: Options, is_stub: bool, errors: Errors) -> None:
        # 'C' for class, 'F' for function
        self.class_and_function_stack: List[Literal["C", "F"]] = []
        self.imports: List[ImportBase] = []

        self.options = options
        self.is_stub = is_stub
        self.errors = errors

        self.type_ignores: Dict[int, List[str]] = {}

        # Cache of visit_X methods keyed by type of visited object
        self.visitor_cache: Dict[type, Callable[[Optional[AST]], Any]] = {}

    def note(self, msg: str, line: int, column: int) -> None:
        self.errors.report(line, column, msg, severity="note", code=codes.SYNTAX)

    def fail(
        self,
        msg: str,
        line: int,
        column: int,
        blocker: bool = True,
        code: codes.ErrorCode = codes.SYNTAX,
    ) -> None:
        if blocker or not self.options.ignore_errors:
            self.errors.report(line, column, msg, blocker=blocker, code=code)

    def fail_merge_overload(self, node: IfStmt) -> None:
        self.fail(
            "Condition can't be inferred, unable to merge overloads",
            line=node.line,
            column=node.column,
            blocker=False,
            code=codes.MISC,
        )

    def visit(self, node: Optional[AST]) -> Any:
        if node is None:
            return None
        typeobj = type(node)
        visitor = self.visitor_cache.get(typeobj)
        if visitor is None:
            method = "visit_" + node.__class__.__name__
            visitor = getattr(self, method)
            self.visitor_cache[typeobj] = visitor
        return visitor(node)

    def set_line(self, node: N, n: AstNode) -> N:
        node.line = n.lineno
        node.column = n.col_offset
        node.end_line = getattr(n, "end_lineno", None)
        node.end_column = getattr(n, "end_col_offset", None)

        return node

    def translate_opt_expr_list(self, l: Sequence[Optional[AST]]) -> List[Optional[Expression]]:
        res: List[Optional[Expression]] = []
        for e in l:
            exp = self.visit(e)
            res.append(exp)
        return res

    def translate_expr_list(self, l: Sequence[AST]) -> List[Expression]:
        return cast(List[Expression], self.translate_opt_expr_list(l))

    def get_lineno(self, node: Union[ast3.expr, ast3.stmt]) -> int:
        if (
            isinstance(node, (ast3.AsyncFunctionDef, ast3.ClassDef, ast3.FunctionDef))
            and node.decorator_list
        ):
            return node.decorator_list[0].lineno
        return node.lineno

    def translate_stmt_list(
        self, stmts: Sequence[ast3.stmt], ismodule: bool = False
    ) -> List[Statement]:
        # A "# type: ignore" comment before the first statement of a module
        # ignores the whole module:
        if (
            ismodule
            and stmts
            and self.type_ignores
            and min(self.type_ignores) < self.get_lineno(stmts[0])
        ):
            self.errors.used_ignored_lines[self.errors.file][min(self.type_ignores)].append(
                codes.FILE.code
            )
            block = Block(self.fix_function_overloads(self.translate_stmt_list(stmts)))
            mark_block_unreachable(block)
            return [block]

        res: List[Statement] = []
        for stmt in stmts:
            node = self.visit(stmt)
            res.append(node)

        return res

    def translate_type_comment(
        self, n: Union[ast3.stmt, ast3.arg], type_comment: Optional[str]
    ) -> Optional[ProperType]:
        if type_comment is None:
            return None
        else:
            lineno = n.lineno
            extra_ignore, typ = parse_type_comment(type_comment, lineno, n.col_offset, self.errors)
            if extra_ignore is not None:
                self.type_ignores[lineno] = extra_ignore
            return typ

    op_map: Final[Dict[typing.Type[AST], str]] = {
        ast3.Add: "+",
        ast3.Sub: "-",
        ast3.Mult: "*",
        ast3.MatMult: "@",
        ast3.Div: "/",
        ast3.Mod: "%",
        ast3.Pow: "**",
        ast3.LShift: "<<",
        ast3.RShift: ">>",
        ast3.BitOr: "|",
        ast3.BitXor: "^",
        ast3.BitAnd: "&",
        ast3.FloorDiv: "//",
    }

    def from_operator(self, op: ast3.operator) -> str:
        op_name = ASTConverter.op_map.get(type(op))
        if op_name is None:
            raise RuntimeError("Unknown operator " + str(type(op)))
        else:
            return op_name

    comp_op_map: Final[Dict[typing.Type[AST], str]] = {
        ast3.Gt: ">",
        ast3.Lt: "<",
        ast3.Eq: "==",
        ast3.GtE: ">=",
        ast3.LtE: "<=",
        ast3.NotEq: "!=",
        ast3.Is: "is",
        ast3.IsNot: "is not",
        ast3.In: "in",
        ast3.NotIn: "not in",
    }

    def from_comp_operator(self, op: ast3.cmpop) -> str:
        op_name = ASTConverter.comp_op_map.get(type(op))
        if op_name is None:
            raise RuntimeError("Unknown comparison operator " + str(type(op)))
        else:
            return op_name

    def as_block(self, stmts: List[ast3.stmt], lineno: int) -> Optional[Block]:
        b = None
        if stmts:
            b = Block(self.fix_function_overloads(self.translate_stmt_list(stmts)))
            b.set_line(lineno)
        return b

    def as_required_block(self, stmts: List[ast3.stmt], lineno: int) -> Block:
        assert stmts  # must be non-empty
        b = Block(self.fix_function_overloads(self.translate_stmt_list(stmts)))
        # TODO: in most call sites line is wrong (includes first line of enclosing statement)
        # TODO: also we need to set the column, and the end position here.
        b.set_line(lineno)
        return b

    def fix_function_overloads(self, stmts: List[Statement]) -> List[Statement]:
        ret: List[Statement] = []
        current_overload: List[OverloadPart] = []
        current_overload_name: Optional[str] = None
        seen_unconditional_func_def = False
        last_if_stmt: Optional[IfStmt] = None
        last_if_overload: Optional[Union[Decorator, FuncDef, OverloadedFuncDef]] = None
        last_if_stmt_overload_name: Optional[str] = None
        last_if_unknown_truth_value: Optional[IfStmt] = None
        skipped_if_stmts: List[IfStmt] = []
        for stmt in stmts:
            if_overload_name: Optional[str] = None
            if_block_with_overload: Optional[Block] = None
            if_unknown_truth_value: Optional[IfStmt] = None
            if isinstance(stmt, IfStmt) and seen_unconditional_func_def is False:
                # Check IfStmt block to determine if function overloads can be merged
                if_overload_name = self._check_ifstmt_for_overloads(stmt, current_overload_name)
                if if_overload_name is not None:
                    (
                        if_block_with_overload,
                        if_unknown_truth_value,
                    ) = self._get_executable_if_block_with_overloads(stmt)

            if (
                current_overload_name is not None
                and isinstance(stmt, (Decorator, FuncDef))
                and stmt.name == current_overload_name
            ):
                if last_if_stmt is not None:
                    skipped_if_stmts.append(last_if_stmt)
                if last_if_overload is not None:
                    # Last stmt was an IfStmt with same overload name
                    # Add overloads to current_overload
                    if isinstance(last_if_overload, OverloadedFuncDef):
                        current_overload.extend(last_if_overload.items)
                    else:
                        current_overload.append(last_if_overload)
                    last_if_stmt, last_if_overload = None, None
                if last_if_unknown_truth_value:
                    self.fail_merge_overload(last_if_unknown_truth_value)
                    last_if_unknown_truth_value = None
                current_overload.append(stmt)
                if isinstance(stmt, FuncDef):
                    seen_unconditional_func_def = True
            elif (
                current_overload_name is not None
                and isinstance(stmt, IfStmt)
                and if_overload_name == current_overload_name
            ):
                # IfStmt only contains stmts relevant to current_overload.
                # Check if stmts are reachable and add them to current_overload,
                # otherwise skip IfStmt to allow subsequent overload
                # or function definitions.
                skipped_if_stmts.append(stmt)
                if if_block_with_overload is None:
                    if if_unknown_truth_value is not None:
                        self.fail_merge_overload(if_unknown_truth_value)
                    continue
                if last_if_overload is not None:
                    # Last stmt was an IfStmt with same overload name
                    # Add overloads to current_overload
                    if isinstance(last_if_overload, OverloadedFuncDef):
                        current_overload.extend(last_if_overload.items)
                    else:
                        current_overload.append(last_if_overload)
                    last_if_stmt, last_if_overload = None, None
                if isinstance(if_block_with_overload.body[-1], OverloadedFuncDef):
                    skipped_if_stmts.extend(cast(List[IfStmt], if_block_with_overload.body[:-1]))
                    current_overload.extend(if_block_with_overload.body[-1].items)
                else:
                    current_overload.append(
                        cast(Union[Decorator, FuncDef], if_block_with_overload.body[0])
                    )
            else:
                if last_if_stmt is not None:
                    ret.append(last_if_stmt)
                    last_if_stmt_overload_name = current_overload_name
                    last_if_stmt, last_if_overload = None, None
                    last_if_unknown_truth_value = None

                if current_overload and current_overload_name == last_if_stmt_overload_name:
                    # Remove last stmt (IfStmt) from ret if the overload names matched
                    # Only happens if no executable block had been found in IfStmt
                    skipped_if_stmts.append(cast(IfStmt, ret.pop()))
                if current_overload and skipped_if_stmts:
                    # Add bare IfStmt (without overloads) to ret
                    # Required for mypy to be able to still check conditions
                    for if_stmt in skipped_if_stmts:
                        self._strip_contents_from_if_stmt(if_stmt)
                        ret.append(if_stmt)
                    skipped_if_stmts = []
                if len(current_overload) == 1:
                    ret.append(current_overload[0])
                elif len(current_overload) > 1:
                    ret.append(OverloadedFuncDef(current_overload))

                # If we have multiple decorated functions named "_" next to each, we want to treat
                # them as a series of regular FuncDefs instead of one OverloadedFuncDef because
                # most of mypy/mypyc assumes that all the functions in an OverloadedFuncDef are
                # related, but multiple underscore functions next to each other aren't necessarily
                # related
                seen_unconditional_func_def = False
                if isinstance(stmt, Decorator) and not unnamed_function(stmt.name):
                    current_overload = [stmt]
                    current_overload_name = stmt.name
                elif isinstance(stmt, IfStmt) and if_overload_name is not None:
                    current_overload = []
                    current_overload_name = if_overload_name
                    last_if_stmt = stmt
                    last_if_stmt_overload_name = None
                    if if_block_with_overload is not None:
                        skipped_if_stmts.extend(
                            cast(List[IfStmt], if_block_with_overload.body[:-1])
                        )
                        last_if_overload = cast(
                            Union[Decorator, FuncDef, OverloadedFuncDef],
                            if_block_with_overload.body[-1],
                        )
                    last_if_unknown_truth_value = if_unknown_truth_value
                else:
                    current_overload = []
                    current_overload_name = None
                    ret.append(stmt)

        if current_overload and skipped_if_stmts:
            # Add bare IfStmt (without overloads) to ret
            # Required for mypy to be able to still check conditions
            for if_stmt in skipped_if_stmts:
                self._strip_contents_from_if_stmt(if_stmt)
                ret.append(if_stmt)
        if len(current_overload) == 1:
            ret.append(current_overload[0])
        elif len(current_overload) > 1:
            ret.append(OverloadedFuncDef(current_overload))
        elif last_if_overload is not None:
            ret.append(last_if_overload)
        elif last_if_stmt is not None:
            ret.append(last_if_stmt)
        return ret

    def _check_ifstmt_for_overloads(
        self, stmt: IfStmt, current_overload_name: Optional[str] = None
    ) -> Optional[str]:
        """Check if IfStmt contains only overloads with the same name.
        Return overload_name if found, None otherwise.
        """
        # Check that block only contains a single Decorator, FuncDef, or OverloadedFuncDef.
        # Multiple overloads have already been merged as OverloadedFuncDef.
        if not (
            len(stmt.body[0].body) == 1
            and (
                isinstance(stmt.body[0].body[0], (Decorator, OverloadedFuncDef))
                or current_overload_name is not None
                and isinstance(stmt.body[0].body[0], FuncDef)
            )
            or len(stmt.body[0].body) > 1
            and isinstance(stmt.body[0].body[-1], OverloadedFuncDef)
            and all(self._is_stripped_if_stmt(if_stmt) for if_stmt in stmt.body[0].body[:-1])
        ):
            return None

        overload_name = cast(
            Union[Decorator, FuncDef, OverloadedFuncDef], stmt.body[0].body[-1]
        ).name
        if stmt.else_body is None:
            return overload_name

        if isinstance(stmt.else_body, Block) and len(stmt.else_body.body) == 1:
            # For elif: else_body contains an IfStmt itself -> do a recursive check.
            if (
                isinstance(stmt.else_body.body[0], (Decorator, FuncDef, OverloadedFuncDef))
                and stmt.else_body.body[0].name == overload_name
            ):
                return overload_name
            if (
                isinstance(stmt.else_body.body[0], IfStmt)
                and self._check_ifstmt_for_overloads(stmt.else_body.body[0], current_overload_name)
                == overload_name
            ):
                return overload_name

        return None

    def _get_executable_if_block_with_overloads(
        self, stmt: IfStmt
    ) -> Tuple[Optional[Block], Optional[IfStmt]]:
        """Return block from IfStmt that will get executed.

        Return
            0 -> A block if sure that alternative blocks are unreachable.
            1 -> An IfStmt if the reachability of it can't be inferred,
                 i.e. the truth value is unknown.
        """
        infer_reachability_of_if_statement(stmt, self.options)
        if stmt.else_body is None and stmt.body[0].is_unreachable is True:
            # always False condition with no else
            return None, None
        if (
            stmt.else_body is None
            or stmt.body[0].is_unreachable is False
            and stmt.else_body.is_unreachable is False
        ):
            # The truth value is unknown, thus not conclusive
            return None, stmt
        if stmt.else_body.is_unreachable is True:
            # else_body will be set unreachable if condition is always True
            return stmt.body[0], None
        if stmt.body[0].is_unreachable is True:
            # body will be set unreachable if condition is always False
            # else_body can contain an IfStmt itself (for elif) -> do a recursive check
            if isinstance(stmt.else_body.body[0], IfStmt):
                return self._get_executable_if_block_with_overloads(stmt.else_body.body[0])
            return stmt.else_body, None
        return None, stmt

    def _strip_contents_from_if_stmt(self, stmt: IfStmt) -> None:
        """Remove contents from IfStmt.

        Needed to still be able to check the conditions after the contents
        have been merged with the surrounding function overloads.
        """
        if len(stmt.body) == 1:
            stmt.body[0].body = []
        if stmt.else_body and len(stmt.else_body.body) == 1:
            if isinstance(stmt.else_body.body[0], IfStmt):
                self._strip_contents_from_if_stmt(stmt.else_body.body[0])
            else:
                stmt.else_body.body = []

    def _is_stripped_if_stmt(self, stmt: Statement) -> bool:
        """Check stmt to make sure it is a stripped IfStmt.

        See also: _strip_contents_from_if_stmt
        """
        if not isinstance(stmt, IfStmt):
            return False

        if not (len(stmt.body) == 1 and len(stmt.body[0].body) == 0):
            # Body not empty
            return False

        if not stmt.else_body or len(stmt.else_body.body) == 0:
            # No or empty else_body
            return True

        # For elif, IfStmt are stored recursively in else_body
        return self._is_stripped_if_stmt(stmt.else_body.body[0])

    def in_method_scope(self) -> bool:
        return self.class_and_function_stack[-2:] == ["C", "F"]

    def translate_module_id(self, id: str) -> str:
        """Return the actual, internal module id for a source text id."""
        if id == self.options.custom_typing_module:
            return "typing"
        return id

    def visit_Module(self, mod: ast3.Module) -> MypyFile:
        self.type_ignores = {}
        for ti in mod.type_ignores:
            parsed = parse_type_ignore_tag(ti.tag)  # type: ignore[attr-defined]
            if parsed is not None:
                self.type_ignores[ti.lineno] = parsed
            else:
                self.fail(INVALID_TYPE_IGNORE, ti.lineno, -1)
        body = self.fix_function_overloads(self.translate_stmt_list(mod.body, ismodule=True))
        return MypyFile(body, self.imports, False, self.type_ignores)

    # --- stmt ---
    # FunctionDef(identifier name, arguments args,
    #             stmt* body, expr* decorator_list, expr? returns, string? type_comment)
    # arguments = (arg* args, arg? vararg, arg* kwonlyargs, expr* kw_defaults,
    #              arg? kwarg, expr* defaults)
    def visit_FunctionDef(self, n: ast3.FunctionDef) -> Union[FuncDef, Decorator]:
        return self.do_func_def(n)

    # AsyncFunctionDef(identifier name, arguments args,
    #                  stmt* body, expr* decorator_list, expr? returns, string? type_comment)
    def visit_AsyncFunctionDef(self, n: ast3.AsyncFunctionDef) -> Union[FuncDef, Decorator]:
        return self.do_func_def(n, is_coroutine=True)

    def do_func_def(
        self, n: Union[ast3.FunctionDef, ast3.AsyncFunctionDef], is_coroutine: bool = False
    ) -> Union[FuncDef, Decorator]:
        """Helper shared between visit_FunctionDef and visit_AsyncFunctionDef."""
        self.class_and_function_stack.append("F")
        no_type_check = bool(
            n.decorator_list and any(is_no_type_check_decorator(d) for d in n.decorator_list)
        )

        lineno = n.lineno
        args = self.transform_args(n.args, lineno, no_type_check=no_type_check)
        if special_function_elide_names(n.name):
            for arg in args:
                arg.pos_only = True

        arg_kinds = [arg.kind for arg in args]
        arg_names = [None if arg.pos_only else arg.variable.name for arg in args]

        arg_types: List[Optional[Type]] = []
        if no_type_check:
            arg_types = [None] * len(args)
            return_type = None
        elif n.type_comment is not None:
            try:
                func_type_ast = ast3_parse(n.type_comment, "<func_type>", "func_type")
                assert isinstance(func_type_ast, FunctionType)
                # for ellipsis arg
                if len(func_type_ast.argtypes) == 1 and isinstance(
                    func_type_ast.argtypes[0], ast3_Ellipsis
                ):
                    if n.returns:
                        # PEP 484 disallows both type annotations and type comments
                        self.fail(message_registry.DUPLICATE_TYPE_SIGNATURES, lineno, n.col_offset)
                    arg_types = [
                        a.type_annotation
                        if a.type_annotation is not None
                        else AnyType(TypeOfAny.unannotated)
                        for a in args
                    ]
                else:
                    # PEP 484 disallows both type annotations and type comments
                    if n.returns or any(a.type_annotation is not None for a in args):
                        self.fail(message_registry.DUPLICATE_TYPE_SIGNATURES, lineno, n.col_offset)
                    translated_args = TypeConverter(
                        self.errors, line=lineno, override_column=n.col_offset
                    ).translate_expr_list(func_type_ast.argtypes)
                    arg_types = [
                        a if a is not None else AnyType(TypeOfAny.unannotated)
                        for a in translated_args
                    ]
                return_type = TypeConverter(self.errors, line=lineno).visit(func_type_ast.returns)

                # add implicit self type
                if self.in_method_scope() and len(arg_types) < len(args):
                    arg_types.insert(0, AnyType(TypeOfAny.special_form))
            except SyntaxError:
                stripped_type = n.type_comment.split("#", 2)[0].strip()
                err_msg = f'{TYPE_COMMENT_SYNTAX_ERROR} "{stripped_type}"'
                self.fail(err_msg, lineno, n.col_offset)
                if n.type_comment and n.type_comment[0] not in ["(", "#"]:
                    self.note(
                        "Suggestion: wrap argument types in parentheses", lineno, n.col_offset
                    )
                arg_types = [AnyType(TypeOfAny.from_error)] * len(args)
                return_type = AnyType(TypeOfAny.from_error)
        else:
            arg_types = [a.type_annotation for a in args]
            return_type = TypeConverter(
                self.errors, line=n.returns.lineno if n.returns else lineno
            ).visit(n.returns)

        for arg, arg_type in zip(args, arg_types):
            self.set_type_optional(arg_type, arg.initializer)

        func_type = None
        if any(arg_types) or return_type:
            if len(arg_types) != 1 and any(isinstance(t, EllipsisType) for t in arg_types):
                self.fail(
                    "Ellipses cannot accompany other argument types " "in function type signature",
                    lineno,
                    n.col_offset,
                )
            elif len(arg_types) > len(arg_kinds):
                self.fail(
                    "Type signature has too many arguments", lineno, n.col_offset, blocker=False
                )
            elif len(arg_types) < len(arg_kinds):
                self.fail(
                    "Type signature has too few arguments", lineno, n.col_offset, blocker=False
                )
            else:
                func_type = CallableType(
                    [a if a is not None else AnyType(TypeOfAny.unannotated) for a in arg_types],
                    arg_kinds,
                    arg_names,
                    return_type if return_type is not None else AnyType(TypeOfAny.unannotated),
                    _dummy_fallback,
                )

        # End position is always the same.
        end_line = getattr(n, "end_lineno", None)
        end_column = getattr(n, "end_col_offset", None)

        func_def = FuncDef(n.name, args, self.as_required_block(n.body, lineno), func_type)
        if isinstance(func_def.type, CallableType):
            # semanal.py does some in-place modifications we want to avoid
            func_def.unanalyzed_type = func_def.type.copy_modified()
        if is_coroutine:
            func_def.is_coroutine = True
        if func_type is not None:
            func_type.definition = func_def
            func_type.line = lineno

        if n.decorator_list:
            if sys.version_info < (3, 8):
                # Before 3.8, [typed_]ast the line number points to the first decorator.
                # In 3.8, it points to the 'def' line, where we want it.
                deco_line = lineno
                lineno += len(n.decorator_list)  # this is only approximately true
            else:
                # Set deco_line to the old pre-3.8 lineno, in order to keep
                # existing "# type: ignore" comments working:
                deco_line = n.decorator_list[0].lineno

            var = Var(func_def.name)
            var.is_ready = False
            var.set_line(lineno)

            func_def.is_decorated = True
            func_def.deco_line = deco_line
            func_def.set_line(lineno, n.col_offset, end_line, end_column)
            # Set the line again after we updated it (to make value same in Python 3.7/3.8)
            # Note that TODOs in as_required_block() apply here as well.
            func_def.body.set_line(lineno)

            deco = Decorator(func_def, self.translate_expr_list(n.decorator_list), var)
            first = n.decorator_list[0]
            deco.set_line(first.lineno, first.col_offset, end_line, end_column)
            retval: Union[FuncDef, Decorator] = deco
        else:
            # FuncDef overrides set_line -- can't use self.set_line
            func_def.set_line(lineno, n.col_offset, end_line, end_column)
            retval = func_def
        self.class_and_function_stack.pop()
        return retval

    def set_type_optional(self, type: Optional[Type], initializer: Optional[Expression]) -> None:
        if self.options.no_implicit_optional:
            return
        # Indicate that type should be wrapped in an Optional if arg is initialized to None.
        optional = isinstance(initializer, NameExpr) and initializer.name == "None"
        if isinstance(type, UnboundType):
            type.optional = optional

    def transform_args(
        self, args: ast3.arguments, line: int, no_type_check: bool = False
    ) -> List[Argument]:
        new_args = []
        names: List[ast3.arg] = []
        posonlyargs = getattr(args, "posonlyargs", cast(List[ast3.arg], []))
        args_args = posonlyargs + args.args
        args_defaults = args.defaults
        num_no_defaults = len(args_args) - len(args_defaults)
        # positional arguments without defaults
        for i, a in enumerate(args_args[:num_no_defaults]):
            pos_only = i < len(posonlyargs)
            new_args.append(self.make_argument(a, None, ARG_POS, no_type_check, pos_only))
            names.append(a)

        # positional arguments with defaults
        for i, (a, d) in enumerate(zip(args_args[num_no_defaults:], args_defaults)):
            pos_only = num_no_defaults + i < len(posonlyargs)
            new_args.append(self.make_argument(a, d, ARG_OPT, no_type_check, pos_only))
            names.append(a)

        # *arg
        if args.vararg is not None:
            new_args.append(self.make_argument(args.vararg, None, ARG_STAR, no_type_check))
            names.append(args.vararg)

        # keyword-only arguments with defaults
        for a, kd in zip(args.kwonlyargs, args.kw_defaults):
            new_args.append(
                self.make_argument(
                    a, kd, ARG_NAMED if kd is None else ARG_NAMED_OPT, no_type_check
                )
            )
            names.append(a)

        # **kwarg
        if args.kwarg is not None:
            new_args.append(self.make_argument(args.kwarg, None, ARG_STAR2, no_type_check))
            names.append(args.kwarg)

        check_arg_names([arg.variable.name for arg in new_args], names, self.fail_arg)

        return new_args

    def make_argument(
        self,
        arg: ast3.arg,
        default: Optional[ast3.expr],
        kind: ArgKind,
        no_type_check: bool,
        pos_only: bool = False,
    ) -> Argument:
        if no_type_check:
            arg_type = None
        else:
            annotation = arg.annotation
            type_comment = arg.type_comment
            if annotation is not None and type_comment is not None:
                self.fail(message_registry.DUPLICATE_TYPE_SIGNATURES, arg.lineno, arg.col_offset)
            arg_type = None
            if annotation is not None:
                arg_type = TypeConverter(self.errors, line=arg.lineno).visit(annotation)
            else:
                arg_type = self.translate_type_comment(arg, type_comment)
        if argument_elide_name(arg.arg):
            pos_only = True

        return Argument(Var(arg.arg), arg_type, self.visit(default), kind, pos_only)

    def fail_arg(self, msg: str, arg: ast3.arg) -> None:
        self.fail(msg, arg.lineno, arg.col_offset)

    # ClassDef(identifier name,
    #  expr* bases,
    #  keyword* keywords,
    #  stmt* body,
    #  expr* decorator_list)
    def visit_ClassDef(self, n: ast3.ClassDef) -> ClassDef:
        self.class_and_function_stack.append("C")
        keywords = [(kw.arg, self.visit(kw.value)) for kw in n.keywords if kw.arg]

        cdef = ClassDef(
            n.name,
            self.as_required_block(n.body, n.lineno),
            None,
            self.translate_expr_list(n.bases),
            metaclass=dict(keywords).get("metaclass"),
            keywords=keywords,
        )
        cdef.decorators = self.translate_expr_list(n.decorator_list)
        # Set lines to match the old mypy 0.700 lines, in order to keep
        # existing "# type: ignore" comments working:
        if sys.version_info < (3, 8):
            cdef.line = n.lineno + len(n.decorator_list)
            cdef.deco_line = n.lineno
        else:
            cdef.line = n.lineno
            cdef.deco_line = n.decorator_list[0].lineno if n.decorator_list else None
        cdef.column = n.col_offset
        cdef.end_line = getattr(n, "end_lineno", None)
        cdef.end_column = getattr(n, "end_col_offset", None)
        self.class_and_function_stack.pop()
        return cdef

    # Return(expr? value)
    def visit_Return(self, n: ast3.Return) -> ReturnStmt:
        node = ReturnStmt(self.visit(n.value))
        return self.set_line(node, n)

    # Delete(expr* targets)
    def visit_Delete(self, n: ast3.Delete) -> DelStmt:
        if len(n.targets) > 1:
            tup = TupleExpr(self.translate_expr_list(n.targets))
            tup.set_line(n.lineno)
            node = DelStmt(tup)
        else:
            node = DelStmt(self.visit(n.targets[0]))
        return self.set_line(node, n)

    # Assign(expr* targets, expr? value, string? type_comment, expr? annotation)
    def visit_Assign(self, n: ast3.Assign) -> AssignmentStmt:
        lvalues = self.translate_expr_list(n.targets)
        rvalue = self.visit(n.value)
        typ = self.translate_type_comment(n, n.type_comment)
        s = AssignmentStmt(lvalues, rvalue, type=typ, new_syntax=False)
        return self.set_line(s, n)

    # AnnAssign(expr target, expr annotation, expr? value, int simple)
    def visit_AnnAssign(self, n: ast3.AnnAssign) -> AssignmentStmt:
        line = n.lineno
        if n.value is None:  # always allow 'x: int'
            rvalue: Expression = TempNode(AnyType(TypeOfAny.special_form), no_rhs=True)
            rvalue.line = line
            rvalue.column = n.col_offset
        else:
            rvalue = self.visit(n.value)
        typ = TypeConverter(self.errors, line=line).visit(n.annotation)
        assert typ is not None
        typ.column = n.annotation.col_offset
        s = AssignmentStmt([self.visit(n.target)], rvalue, type=typ, new_syntax=True)
        return self.set_line(s, n)

    # AugAssign(expr target, operator op, expr value)
    def visit_AugAssign(self, n: ast3.AugAssign) -> OperatorAssignmentStmt:
        s = OperatorAssignmentStmt(
            self.from_operator(n.op), self.visit(n.target), self.visit(n.value)
        )
        return self.set_line(s, n)

    # For(expr target, expr iter, stmt* body, stmt* orelse, string? type_comment)
    def visit_For(self, n: ast3.For) -> ForStmt:
        target_type = self.translate_type_comment(n, n.type_comment)
        node = ForStmt(
            self.visit(n.target),
            self.visit(n.iter),
            self.as_required_block(n.body, n.lineno),
            self.as_block(n.orelse, n.lineno),
            target_type,
        )
        return self.set_line(node, n)

    # AsyncFor(expr target, expr iter, stmt* body, stmt* orelse, string? type_comment)
    def visit_AsyncFor(self, n: ast3.AsyncFor) -> ForStmt:
        target_type = self.translate_type_comment(n, n.type_comment)
        node = ForStmt(
            self.visit(n.target),
            self.visit(n.iter),
            self.as_required_block(n.body, n.lineno),
            self.as_block(n.orelse, n.lineno),
            target_type,
        )
        node.is_async = True
        return self.set_line(node, n)

    # While(expr test, stmt* body, stmt* orelse)
    def visit_While(self, n: ast3.While) -> WhileStmt:
        node = WhileStmt(
            self.visit(n.test),
            self.as_required_block(n.body, n.lineno),
            self.as_block(n.orelse, n.lineno),
        )
        return self.set_line(node, n)

    # If(expr test, stmt* body, stmt* orelse)
    def visit_If(self, n: ast3.If) -> IfStmt:
        lineno = n.lineno
        node = IfStmt(
            [self.visit(n.test)],
            [self.as_required_block(n.body, lineno)],
            self.as_block(n.orelse, lineno),
        )
        return self.set_line(node, n)

    # With(withitem* items, stmt* body, string? type_comment)
    def visit_With(self, n: ast3.With) -> WithStmt:
        target_type = self.translate_type_comment(n, n.type_comment)
        node = WithStmt(
            [self.visit(i.context_expr) for i in n.items],
            [self.visit(i.optional_vars) for i in n.items],
            self.as_required_block(n.body, n.lineno),
            target_type,
        )
        return self.set_line(node, n)

    # AsyncWith(withitem* items, stmt* body, string? type_comment)
    def visit_AsyncWith(self, n: ast3.AsyncWith) -> WithStmt:
        target_type = self.translate_type_comment(n, n.type_comment)
        s = WithStmt(
            [self.visit(i.context_expr) for i in n.items],
            [self.visit(i.optional_vars) for i in n.items],
            self.as_required_block(n.body, n.lineno),
            target_type,
        )
        s.is_async = True
        return self.set_line(s, n)

    # Raise(expr? exc, expr? cause)
    def visit_Raise(self, n: ast3.Raise) -> RaiseStmt:
        node = RaiseStmt(self.visit(n.exc), self.visit(n.cause))
        return self.set_line(node, n)

    # Try(stmt* body, excepthandler* handlers, stmt* orelse, stmt* finalbody)
    def visit_Try(self, n: ast3.Try) -> TryStmt:
        vs = [
            self.set_line(NameExpr(h.name), h) if h.name is not None else None for h in n.handlers
        ]
        types = [self.visit(h.type) for h in n.handlers]
        handlers = [self.as_required_block(h.body, h.lineno) for h in n.handlers]

        node = TryStmt(
            self.as_required_block(n.body, n.lineno),
            vs,
            types,
            handlers,
            self.as_block(n.orelse, n.lineno),
            self.as_block(n.finalbody, n.lineno),
        )
        return self.set_line(node, n)

    # Assert(expr test, expr? msg)
    def visit_Assert(self, n: ast3.Assert) -> AssertStmt:
        node = AssertStmt(self.visit(n.test), self.visit(n.msg))
        return self.set_line(node, n)

    # Import(alias* names)
    def visit_Import(self, n: ast3.Import) -> Import:
        names: List[Tuple[str, Optional[str]]] = []
        for alias in n.names:
            name = self.translate_module_id(alias.name)
            asname = alias.asname
            if asname is None and name != alias.name:
                # if the module name has been translated (and it's not already
                # an explicit import-as), make it an implicit import-as the
                # original name
                asname = alias.name
            names.append((name, asname))
        i = Import(names)
        self.imports.append(i)
        return self.set_line(i, n)

    # ImportFrom(identifier? module, alias* names, int? level)
    def visit_ImportFrom(self, n: ast3.ImportFrom) -> ImportBase:
        assert n.level is not None
        if len(n.names) == 1 and n.names[0].name == "*":
            mod = n.module if n.module is not None else ""
            i: ImportBase = ImportAll(mod, n.level)
        else:
            i = ImportFrom(
                self.translate_module_id(n.module) if n.module is not None else "",
                n.level,
                [(a.name, a.asname) for a in n.names],
            )
        self.imports.append(i)
        return self.set_line(i, n)

    # Global(identifier* names)
    def visit_Global(self, n: ast3.Global) -> GlobalDecl:
        g = GlobalDecl(n.names)
        return self.set_line(g, n)

    # Nonlocal(identifier* names)
    def visit_Nonlocal(self, n: ast3.Nonlocal) -> NonlocalDecl:
        d = NonlocalDecl(n.names)
        return self.set_line(d, n)

    # Expr(expr value)
    def visit_Expr(self, n: ast3.Expr) -> ExpressionStmt:
        value = self.visit(n.value)
        node = ExpressionStmt(value)
        return self.set_line(node, n)

    # Pass
    def visit_Pass(self, n: ast3.Pass) -> PassStmt:
        s = PassStmt()
        return self.set_line(s, n)

    # Break
    def visit_Break(self, n: ast3.Break) -> BreakStmt:
        s = BreakStmt()
        return self.set_line(s, n)

    # Continue
    def visit_Continue(self, n: ast3.Continue) -> ContinueStmt:
        s = ContinueStmt()
        return self.set_line(s, n)

    # --- expr ---

    def visit_NamedExpr(self, n: NamedExpr) -> AssignmentExpr:
        s = AssignmentExpr(self.visit(n.target), self.visit(n.value))
        return self.set_line(s, n)

    # BoolOp(boolop op, expr* values)
    def visit_BoolOp(self, n: ast3.BoolOp) -> OpExpr:
        # mypy translates (1 and 2 and 3) as (1 and (2 and 3))
        assert len(n.values) >= 2
        op_node = n.op
        if isinstance(op_node, ast3.And):
            op = "and"
        elif isinstance(op_node, ast3.Or):
            op = "or"
        else:
            raise RuntimeError("unknown BoolOp " + str(type(n)))

        # potentially inefficient!
        return self.group(op, self.translate_expr_list(n.values), n)

    def group(self, op: str, vals: List[Expression], n: ast3.expr) -> OpExpr:
        if len(vals) == 2:
            e = OpExpr(op, vals[0], vals[1])
        else:
            e = OpExpr(op, vals[0], self.group(op, vals[1:], n))
        return self.set_line(e, n)

    # BinOp(expr left, operator op, expr right)
    def visit_BinOp(self, n: ast3.BinOp) -> OpExpr:
        op = self.from_operator(n.op)

        if op is None:
            raise RuntimeError("cannot translate BinOp " + str(type(n.op)))

        e = OpExpr(op, self.visit(n.left), self.visit(n.right))
        return self.set_line(e, n)

    # UnaryOp(unaryop op, expr operand)
    def visit_UnaryOp(self, n: ast3.UnaryOp) -> UnaryExpr:
        op = None
        if isinstance(n.op, ast3.Invert):
            op = "~"
        elif isinstance(n.op, ast3.Not):
            op = "not"
        elif isinstance(n.op, ast3.UAdd):
            op = "+"
        elif isinstance(n.op, ast3.USub):
            op = "-"

        if op is None:
            raise RuntimeError("cannot translate UnaryOp " + str(type(n.op)))

        e = UnaryExpr(op, self.visit(n.operand))
        return self.set_line(e, n)

    # Lambda(arguments args, expr body)
    def visit_Lambda(self, n: ast3.Lambda) -> LambdaExpr:
        body = ast3.Return(n.body)
        body.lineno = n.body.lineno
        body.col_offset = n.body.col_offset

        e = LambdaExpr(
            self.transform_args(n.args, n.lineno), self.as_required_block([body], n.lineno)
        )
        e.set_line(n.lineno, n.col_offset)  # Overrides set_line -- can't use self.set_line
        return e

    # IfExp(expr test, expr body, expr orelse)
    def visit_IfExp(self, n: ast3.IfExp) -> ConditionalExpr:
        e = ConditionalExpr(self.visit(n.test), self.visit(n.body), self.visit(n.orelse))
        return self.set_line(e, n)

    # Dict(expr* keys, expr* values)
    def visit_Dict(self, n: ast3.Dict) -> DictExpr:
        e = DictExpr(
            list(zip(self.translate_opt_expr_list(n.keys), self.translate_expr_list(n.values)))
        )
        return self.set_line(e, n)

    # Set(expr* elts)
    def visit_Set(self, n: ast3.Set) -> SetExpr:
        e = SetExpr(self.translate_expr_list(n.elts))
        return self.set_line(e, n)

    # ListComp(expr elt, comprehension* generators)
    def visit_ListComp(self, n: ast3.ListComp) -> ListComprehension:
        e = ListComprehension(self.visit_GeneratorExp(cast(ast3.GeneratorExp, n)))
        return self.set_line(e, n)

    # SetComp(expr elt, comprehension* generators)
    def visit_SetComp(self, n: ast3.SetComp) -> SetComprehension:
        e = SetComprehension(self.visit_GeneratorExp(cast(ast3.GeneratorExp, n)))
        return self.set_line(e, n)

    # DictComp(expr key, expr value, comprehension* generators)
    def visit_DictComp(self, n: ast3.DictComp) -> DictionaryComprehension:
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.translate_expr_list(c.ifs) for c in n.generators]
        is_async = [bool(c.is_async) for c in n.generators]
        e = DictionaryComprehension(
            self.visit(n.key), self.visit(n.value), targets, iters, ifs_list, is_async
        )
        return self.set_line(e, n)

    # GeneratorExp(expr elt, comprehension* generators)
    def visit_GeneratorExp(self, n: ast3.GeneratorExp) -> GeneratorExpr:
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.translate_expr_list(c.ifs) for c in n.generators]
        is_async = [bool(c.is_async) for c in n.generators]
        e = GeneratorExpr(self.visit(n.elt), targets, iters, ifs_list, is_async)
        return self.set_line(e, n)

    # Await(expr value)
    def visit_Await(self, n: ast3.Await) -> AwaitExpr:
        v = self.visit(n.value)
        e = AwaitExpr(v)
        return self.set_line(e, n)

    # Yield(expr? value)
    def visit_Yield(self, n: ast3.Yield) -> YieldExpr:
        e = YieldExpr(self.visit(n.value))
        return self.set_line(e, n)

    # YieldFrom(expr value)
    def visit_YieldFrom(self, n: ast3.YieldFrom) -> YieldFromExpr:
        e = YieldFromExpr(self.visit(n.value))
        return self.set_line(e, n)

    # Compare(expr left, cmpop* ops, expr* comparators)
    def visit_Compare(self, n: ast3.Compare) -> ComparisonExpr:
        operators = [self.from_comp_operator(o) for o in n.ops]
        operands = self.translate_expr_list([n.left] + n.comparators)
        e = ComparisonExpr(operators, operands)
        return self.set_line(e, n)

    # Call(expr func, expr* args, keyword* keywords)
    # keyword = (identifier? arg, expr value)
    def visit_Call(self, n: Call) -> CallExpr:
        args = n.args
        keywords = n.keywords
        keyword_names = [k.arg for k in keywords]
        arg_types = self.translate_expr_list(
            [a.value if isinstance(a, Starred) else a for a in args] + [k.value for k in keywords]
        )
        arg_kinds = [ARG_STAR if type(a) is Starred else ARG_POS for a in args] + [
            ARG_STAR2 if arg is None else ARG_NAMED for arg in keyword_names
        ]
        e = CallExpr(
            self.visit(n.func),
            arg_types,
            arg_kinds,
            cast("List[Optional[str]]", [None] * len(args)) + keyword_names,
        )
        return self.set_line(e, n)

    # Constant(object value) -- a constant, in Python 3.8.
    def visit_Constant(self, n: Constant) -> Any:
        val = n.value
        e: Any = None
        if val is None:
            e = NameExpr("None")
        elif isinstance(val, str):
            e = StrExpr(n.s)
        elif isinstance(val, bytes):
            e = BytesExpr(bytes_to_human_readable_repr(n.s))
        elif isinstance(val, bool):  # Must check before int!
            e = NameExpr(str(val))
        elif isinstance(val, int):
            e = IntExpr(val)
        elif isinstance(val, float):
            e = FloatExpr(val)
        elif isinstance(val, complex):
            e = ComplexExpr(val)
        elif val is Ellipsis:
            e = EllipsisExpr()
        else:
            raise RuntimeError("Constant not implemented for " + str(type(val)))
        return self.set_line(e, n)

    # Num(object n) -- a number as a PyObject.
    def visit_Num(self, n: ast3.Num) -> Union[IntExpr, FloatExpr, ComplexExpr]:
        # The n field has the type complex, but complex isn't *really*
        # a parent of int and float, and this causes isinstance below
        # to think that the complex branch is always picked. Avoid
        # this by throwing away the type.
        val: object = n.n
        if isinstance(val, int):
            e: Union[IntExpr, FloatExpr, ComplexExpr] = IntExpr(val)
        elif isinstance(val, float):
            e = FloatExpr(val)
        elif isinstance(val, complex):
            e = ComplexExpr(val)
        else:
            raise RuntimeError("num not implemented for " + str(type(val)))
        return self.set_line(e, n)

    # Str(string s)
    def visit_Str(self, n: Str) -> StrExpr:
        e = StrExpr(n.s)
        return self.set_line(e, n)

    # JoinedStr(expr* values)
    def visit_JoinedStr(self, n: ast3.JoinedStr) -> Expression:
        # Each of n.values is a str or FormattedValue; we just concatenate
        # them all using ''.join.
        empty_string = StrExpr("")
        empty_string.set_line(n.lineno, n.col_offset)
        strs_to_join = ListExpr(self.translate_expr_list(n.values))
        strs_to_join.set_line(empty_string)
        # Don't make unnecessary join call if there is only one str to join
        if len(strs_to_join.items) == 1:
            return self.set_line(strs_to_join.items[0], n)
        join_method = MemberExpr(empty_string, "join")
        join_method.set_line(empty_string)
        result_expression = CallExpr(join_method, [strs_to_join], [ARG_POS], [None])
        return self.set_line(result_expression, n)

    # FormattedValue(expr value)
    def visit_FormattedValue(self, n: ast3.FormattedValue) -> Expression:
        # A FormattedValue is a component of a JoinedStr, or it can exist
        # on its own. We translate them to individual '{}'.format(value)
        # calls. Format specifier and conversion information is passed along
        # to allow mypyc to support f-strings with format specifiers and conversions.
        val_exp = self.visit(n.value)
        val_exp.set_line(n.lineno, n.col_offset)
        conv_str = "" if n.conversion is None or n.conversion < 0 else "!" + chr(n.conversion)
        format_string = StrExpr("{" + conv_str + ":{}}")
        format_spec_exp = self.visit(n.format_spec) if n.format_spec is not None else StrExpr("")
        format_string.set_line(n.lineno, n.col_offset)
        format_method = MemberExpr(format_string, "format")
        format_method.set_line(format_string)
        result_expression = CallExpr(
            format_method, [val_exp, format_spec_exp], [ARG_POS, ARG_POS], [None, None]
        )
        return self.set_line(result_expression, n)

    # Bytes(bytes s)
    def visit_Bytes(self, n: ast3.Bytes) -> Union[BytesExpr, StrExpr]:
        e = BytesExpr(bytes_to_human_readable_repr(n.s))
        return self.set_line(e, n)

    # NameConstant(singleton value)
    def visit_NameConstant(self, n: NameConstant) -> NameExpr:
        e = NameExpr(str(n.value))
        return self.set_line(e, n)

    # Ellipsis
    def visit_Ellipsis(self, n: ast3_Ellipsis) -> EllipsisExpr:
        e = EllipsisExpr()
        return self.set_line(e, n)

    # Attribute(expr value, identifier attr, expr_context ctx)
    def visit_Attribute(self, n: Attribute) -> Union[MemberExpr, SuperExpr]:
        value = n.value
        member_expr = MemberExpr(self.visit(value), n.attr)
        obj = member_expr.expr
        if (
            isinstance(obj, CallExpr)
            and isinstance(obj.callee, NameExpr)
            and obj.callee.name == "super"
        ):
            e: Union[MemberExpr, SuperExpr] = SuperExpr(member_expr.name, obj)
        else:
            e = member_expr
        return self.set_line(e, n)

    # Subscript(expr value, slice slice, expr_context ctx)
    def visit_Subscript(self, n: ast3.Subscript) -> IndexExpr:
        e = IndexExpr(self.visit(n.value), self.visit(n.slice))
        self.set_line(e, n)
        # alias to please mypyc
        is_py38_or_earlier = sys.version_info < (3, 9)
        if isinstance(n.slice, ast3.Slice) or (
            is_py38_or_earlier and isinstance(n.slice, ast3.ExtSlice)
        ):
            # Before Python 3.9, Slice has no line/column in the raw ast. To avoid incompatibility
            # visit_Slice doesn't set_line, even in Python 3.9 on.
            # ExtSlice also has no line/column info. In Python 3.9 on, line/column is set for
            # e.index when visiting n.slice.
            e.index.line = e.line
            e.index.column = e.column
        return e

    # Starred(expr value, expr_context ctx)
    def visit_Starred(self, n: Starred) -> StarExpr:
        e = StarExpr(self.visit(n.value))
        return self.set_line(e, n)

    # Name(identifier id, expr_context ctx)
    def visit_Name(self, n: Name) -> NameExpr:
        e = NameExpr(n.id)
        return self.set_line(e, n)

    # List(expr* elts, expr_context ctx)
    def visit_List(self, n: ast3.List) -> Union[ListExpr, TupleExpr]:
        expr_list: List[Expression] = [self.visit(e) for e in n.elts]
        if isinstance(n.ctx, ast3.Store):
            # [x, y] = z and (x, y) = z means exactly the same thing
            e: Union[ListExpr, TupleExpr] = TupleExpr(expr_list)
        else:
            e = ListExpr(expr_list)
        return self.set_line(e, n)

    # Tuple(expr* elts, expr_context ctx)
    def visit_Tuple(self, n: ast3.Tuple) -> TupleExpr:
        e = TupleExpr(self.translate_expr_list(n.elts))
        return self.set_line(e, n)

    # --- slice ---

    # Slice(expr? lower, expr? upper, expr? step)
    def visit_Slice(self, n: ast3.Slice) -> SliceExpr:
        return SliceExpr(self.visit(n.lower), self.visit(n.upper), self.visit(n.step))

    # ExtSlice(slice* dims)
    def visit_ExtSlice(self, n: ast3.ExtSlice) -> TupleExpr:
        # cast for mypyc's benefit on Python 3.9
        return TupleExpr(self.translate_expr_list(cast(Any, n).dims))

    # Index(expr value)
    def visit_Index(self, n: Index) -> Node:
        # cast for mypyc's benefit on Python 3.9
        return self.visit(cast(Any, n).value)

    # Match(expr subject, match_case* cases) # python 3.10 and later
    def visit_Match(self, n: Match) -> MatchStmt:
        node = MatchStmt(
            self.visit(n.subject),
            [self.visit(c.pattern) for c in n.cases],
            [self.visit(c.guard) for c in n.cases],
            [self.as_required_block(c.body, n.lineno) for c in n.cases],
        )
        return self.set_line(node, n)

    def visit_MatchValue(self, n: MatchValue) -> ValuePattern:
        node = ValuePattern(self.visit(n.value))
        return self.set_line(node, n)

    def visit_MatchSingleton(self, n: MatchSingleton) -> SingletonPattern:
        node = SingletonPattern(n.value)
        return self.set_line(node, n)

    def visit_MatchSequence(self, n: MatchSequence) -> SequencePattern:
        patterns = [self.visit(p) for p in n.patterns]
        stars = [p for p in patterns if isinstance(p, StarredPattern)]
        assert len(stars) < 2

        node = SequencePattern(patterns)
        return self.set_line(node, n)

    def visit_MatchStar(self, n: MatchStar) -> StarredPattern:
        if n.name is None:
            node = StarredPattern(None)
        else:
            node = StarredPattern(NameExpr(n.name))

        return self.set_line(node, n)

    def visit_MatchMapping(self, n: MatchMapping) -> MappingPattern:
        keys = [self.visit(k) for k in n.keys]
        values = [self.visit(v) for v in n.patterns]

        if n.rest is None:
            rest = None
        else:
            rest = NameExpr(n.rest)

        node = MappingPattern(keys, values, rest)
        return self.set_line(node, n)

    def visit_MatchClass(self, n: MatchClass) -> ClassPattern:
        class_ref = self.visit(n.cls)
        assert isinstance(class_ref, RefExpr)
        positionals = [self.visit(p) for p in n.patterns]
        keyword_keys = n.kwd_attrs
        keyword_values = [self.visit(p) for p in n.kwd_patterns]

        node = ClassPattern(class_ref, positionals, keyword_keys, keyword_values)
        return self.set_line(node, n)

    # MatchAs(expr pattern, identifier name)
    def visit_MatchAs(self, n: MatchAs) -> AsPattern:
        if n.name is None:
            name = None
        else:
            name = NameExpr(n.name)
            name = self.set_line(name, n)
        node = AsPattern(self.visit(n.pattern), name)
        return self.set_line(node, n)

    # MatchOr(expr* pattern)
    def visit_MatchOr(self, n: MatchOr) -> OrPattern:
        node = OrPattern([self.visit(pattern) for pattern in n.patterns])
        return self.set_line(node, n)


class TypeConverter:
    def __init__(
        self,
        errors: Optional[Errors],
        line: int = -1,
        override_column: int = -1,
        is_evaluated: bool = True,
    ) -> None:
        self.errors = errors
        self.line = line
        self.override_column = override_column
        self.node_stack: List[AST] = []
        self.is_evaluated = is_evaluated

    def convert_column(self, column: int) -> int:
        """Apply column override if defined; otherwise return column.

        Column numbers are sometimes incorrect in the AST and the column
        override can be used to work around that.
        """
        if self.override_column < 0:
            return column
        else:
            return self.override_column

    def invalid_type(self, node: AST, note: Optional[str] = None) -> RawExpressionType:
        """Constructs a type representing some expression that normally forms an invalid type.
        For example, if we see a type hint that says "3 + 4", we would transform that
        expression into a RawExpressionType.

        The semantic analysis layer will report an "Invalid type" error when it
        encounters this type, along with the given note if one is provided.

        See RawExpressionType's docstring for more details on how it's used.
        """
        return RawExpressionType(
            None, "typing.Any", line=self.line, column=getattr(node, "col_offset", -1), note=note
        )

    @overload
    def visit(self, node: ast3.expr) -> ProperType:
        ...

    @overload
    def visit(self, node: Optional[AST]) -> Optional[ProperType]:
        ...

    def visit(self, node: Optional[AST]) -> Optional[ProperType]:
        """Modified visit -- keep track of the stack of nodes"""
        if node is None:
            return None
        self.node_stack.append(node)
        try:
            method = "visit_" + node.__class__.__name__
            visitor = getattr(self, method, None)
            if visitor is not None:
                return visitor(node)
            else:
                return self.invalid_type(node)
        finally:
            self.node_stack.pop()

    def parent(self) -> Optional[AST]:
        """Return the AST node above the one we are processing"""
        if len(self.node_stack) < 2:
            return None
        return self.node_stack[-2]

    def fail(self, msg: str, line: int, column: int) -> None:
        if self.errors:
            self.errors.report(line, column, msg, blocker=True, code=codes.SYNTAX)

    def note(self, msg: str, line: int, column: int) -> None:
        if self.errors:
            self.errors.report(line, column, msg, severity="note", code=codes.SYNTAX)

    def translate_expr_list(self, l: Sequence[ast3.expr]) -> List[Type]:
        return [self.visit(e) for e in l]

    def visit_Call(self, e: Call) -> Type:
        # Parse the arg constructor
        f = e.func
        constructor = stringify_name(f)

        if not isinstance(self.parent(), ast3.List):
            note = None
            if constructor:
                note = "Suggestion: use {0}[...] instead of {0}(...)".format(constructor)
            return self.invalid_type(e, note=note)
        if not constructor:
            self.fail("Expected arg constructor name", e.lineno, e.col_offset)

        name: Optional[str] = None
        default_type = AnyType(TypeOfAny.special_form)
        typ: Type = default_type
        for i, arg in enumerate(e.args):
            if i == 0:
                converted = self.visit(arg)
                assert converted is not None
                typ = converted
            elif i == 1:
                name = self._extract_argument_name(arg)
            else:
                self.fail("Too many arguments for argument constructor", f.lineno, f.col_offset)
        for k in e.keywords:
            value = k.value
            if k.arg == "name":
                if name is not None:
                    self.fail(
                        '"{}" gets multiple values for keyword argument "name"'.format(
                            constructor
                        ),
                        f.lineno,
                        f.col_offset,
                    )
                name = self._extract_argument_name(value)
            elif k.arg == "type":
                if typ is not default_type:
                    self.fail(
                        '"{}" gets multiple values for keyword argument "type"'.format(
                            constructor
                        ),
                        f.lineno,
                        f.col_offset,
                    )
                converted = self.visit(value)
                assert converted is not None
                typ = converted
            else:
                self.fail(
                    f'Unexpected argument "{k.arg}" for argument constructor',
                    value.lineno,
                    value.col_offset,
                )
        return CallableArgument(typ, name, constructor, e.lineno, e.col_offset)

    def translate_argument_list(self, l: Sequence[ast3.expr]) -> TypeList:
        return TypeList([self.visit(e) for e in l], line=self.line)

    def _extract_argument_name(self, n: ast3.expr) -> Optional[str]:
        if isinstance(n, Str):
            return n.s.strip()
        elif isinstance(n, NameConstant) and str(n.value) == "None":
            return None
        self.fail(
            "Expected string literal for argument name, got {}".format(type(n).__name__),
            self.line,
            0,
        )
        return None

    def visit_Name(self, n: Name) -> Type:
        return UnboundType(n.id, line=self.line, column=self.convert_column(n.col_offset))

    def visit_BinOp(self, n: ast3.BinOp) -> Type:
        if not isinstance(n.op, ast3.BitOr):
            return self.invalid_type(n)

        left = self.visit(n.left)
        right = self.visit(n.right)
        return UnionType(
            [left, right],
            line=self.line,
            column=self.convert_column(n.col_offset),
            is_evaluated=self.is_evaluated,
            uses_pep604_syntax=True,
        )

    def visit_NameConstant(self, n: NameConstant) -> Type:
        if isinstance(n.value, bool):
            return RawExpressionType(n.value, "builtins.bool", line=self.line)
        else:
            return UnboundType(str(n.value), line=self.line, column=n.col_offset)

    # Only for 3.8 and newer
    def visit_Constant(self, n: Constant) -> Type:
        val = n.value
        if val is None:
            # None is a type.
            return UnboundType("None", line=self.line)
        if isinstance(val, str):
            # Parse forward reference.
            return parse_type_string(n.s, "builtins.str", self.line, n.col_offset)
        if val is Ellipsis:
            # '...' is valid in some types.
            return EllipsisType(line=self.line)
        if isinstance(val, bool):
            # Special case for True/False.
            return RawExpressionType(val, "builtins.bool", line=self.line)
        if isinstance(val, (int, float, complex)):
            return self.numeric_type(val, n)
        if isinstance(val, bytes):
            contents = bytes_to_human_readable_repr(val)
            return RawExpressionType(contents, "builtins.bytes", self.line, column=n.col_offset)
        # Everything else is invalid.
        return self.invalid_type(n)

    # UnaryOp(op, operand)
    def visit_UnaryOp(self, n: UnaryOp) -> Type:
        # We support specifically Literal[-4] and nothing else.
        # For example, Literal[+4] or Literal[~6] is not supported.
        typ = self.visit(n.operand)
        if isinstance(typ, RawExpressionType) and isinstance(n.op, USub):
            if isinstance(typ.literal_value, int):
                typ.literal_value *= -1
                return typ
        return self.invalid_type(n)

    def numeric_type(self, value: object, n: AST) -> Type:
        # The node's field has the type complex, but complex isn't *really*
        # a parent of int and float, and this causes isinstance below
        # to think that the complex branch is always picked. Avoid
        # this by throwing away the type.
        if isinstance(value, int):
            numeric_value: Optional[int] = value
            type_name = "builtins.int"
        else:
            # Other kinds of numbers (floats, complex) are not valid parameters for
            # RawExpressionType so we just pass in 'None' for now. We'll report the
            # appropriate error at a later stage.
            numeric_value = None
            type_name = f"builtins.{type(value).__name__}"
        return RawExpressionType(
            numeric_value, type_name, line=self.line, column=getattr(n, "col_offset", -1)
        )

    # These next three methods are only used if we are on python <
    # 3.8, using typed_ast.  They are defined unconditionally because
    # mypyc can't handle conditional method definitions.

    # Num(number n)
    def visit_Num(self, n: Num) -> Type:
        return self.numeric_type(n.n, n)

    # Str(string s)
    def visit_Str(self, n: Str) -> Type:
        return parse_type_string(n.s, "builtins.str", self.line, n.col_offset)

    # Bytes(bytes s)
    def visit_Bytes(self, n: Bytes) -> Type:
        contents = bytes_to_human_readable_repr(n.s)
        return RawExpressionType(contents, "builtins.bytes", self.line, column=n.col_offset)

    def visit_Index(self, n: ast3.Index) -> Type:
        # cast for mypyc's benefit on Python 3.9
        return self.visit(cast(Any, n).value)

    def visit_Slice(self, n: ast3.Slice) -> Type:
        return self.invalid_type(n, note="did you mean to use ',' instead of ':' ?")

    # Subscript(expr value, slice slice, expr_context ctx)  # Python 3.8 and before
    # Subscript(expr value, expr slice, expr_context ctx)  # Python 3.9 and later
    def visit_Subscript(self, n: ast3.Subscript) -> Type:
        if sys.version_info >= (3, 9):  # Really 3.9a5 or later
            sliceval: Any = n.slice
        # Python 3.8 or earlier use a different AST structure for subscripts
        elif isinstance(n.slice, ast3.Index):
            sliceval: Any = n.slice.value
        elif isinstance(n.slice, ast3.Slice):
            sliceval = copy.deepcopy(n.slice)  # so we don't mutate passed AST
            if getattr(sliceval, "col_offset", None) is None:
                # Fix column information so that we get Python 3.9+ message order
                sliceval.col_offset = sliceval.lower.col_offset
        else:
            assert isinstance(n.slice, ast3.ExtSlice)
            dims = copy.deepcopy(n.slice.dims)
            for s in dims:
                if getattr(s, "col_offset", None) is None:
                    if isinstance(s, ast3.Index):
                        s.col_offset = s.value.col_offset  # type: ignore
                    elif isinstance(s, ast3.Slice):
                        s.col_offset = s.lower.col_offset  # type: ignore
            sliceval = ast3.Tuple(dims, n.ctx)

        empty_tuple_index = False
        if isinstance(sliceval, ast3.Tuple):
            params = self.translate_expr_list(sliceval.elts)
            if len(sliceval.elts) == 0:
                empty_tuple_index = True
        else:
            params = [self.visit(sliceval)]

        value = self.visit(n.value)
        if isinstance(value, UnboundType) and not value.args:
            return UnboundType(
                value.name,
                params,
                line=self.line,
                column=value.column,
                empty_tuple_index=empty_tuple_index,
            )
        else:
            return self.invalid_type(n)

    def visit_Tuple(self, n: ast3.Tuple) -> Type:
        return TupleType(
            self.translate_expr_list(n.elts),
            _dummy_fallback,
            implicit=True,
            line=self.line,
            column=self.convert_column(n.col_offset),
        )

    # Attribute(expr value, identifier attr, expr_context ctx)
    def visit_Attribute(self, n: Attribute) -> Type:
        before_dot = self.visit(n.value)

        if isinstance(before_dot, UnboundType) and not before_dot.args:
            return UnboundType(f"{before_dot.name}.{n.attr}", line=self.line)
        else:
            return self.invalid_type(n)

    # Ellipsis
    def visit_Ellipsis(self, n: ast3_Ellipsis) -> Type:
        return EllipsisType(line=self.line)

    # List(expr* elts, expr_context ctx)
    def visit_List(self, n: ast3.List) -> Type:
        assert isinstance(n.ctx, ast3.Load)
        return self.translate_argument_list(n.elts)


def stringify_name(n: AST) -> Optional[str]:
    if isinstance(n, Name):
        return n.id
    elif isinstance(n, Attribute):
        sv = stringify_name(n.value)
        if sv is not None:
            return f"{sv}.{n.attr}"
    return None  # Can't do it.
