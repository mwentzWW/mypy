#!/usr/bin/env python3
"""Generator of dynamically typed draft stubs for arbitrary modules.

The logic of this script can be split in three steps:
* parsing options and finding sources:
  - use runtime imports be default (to find also C modules)
  - or use mypy's mechanisms, if importing is prohibited
* (optionally) semantically analysing the sources using mypy (as a single set)
* emitting the stubs text:
  - for Python modules: from ASTs using StubGenerator
  - for C modules using runtime introspection and (optionally) Sphinx docs

During first and third steps some problematic files can be skipped, but any
blocking error during second step will cause the whole program to stop.

Basic usage:

  $ stubgen foo.py bar.py some_directory
  => Generate out/foo.pyi, out/bar.pyi, and stubs for some_directory (recursively).

  $ stubgen -m urllib.parse
  => Generate out/urllib/parse.pyi.

  $ stubgen -p urllib
  => Generate stubs for whole urlib package (recursively).

For C modules, you can get more precise function signatures by parsing .rst (Sphinx)
documentation for extra information. For this, use the --doc-dir option:

  $ stubgen --doc-dir <DIR>/Python-3.4.2/Doc/library -m curses

Note: The generated stubs should be verified manually.

TODO:
 - maybe use .rst docs also for Python modules
 - maybe export more imported names if there is no __all__ (this affects ssl.SSLError, for example)
   - a quick and dirty heuristic would be to turn this on if a module has something like
     'from x import y as _y'
 - we don't seem to always detect properties ('closed' in 'io', for example)
"""

import argparse
import glob
import os
import os.path
import sys
import traceback
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple, Union, cast

from typing_extensions import Final

import mypy.build
import mypy.mixedtraverser
import mypy.parse
import mypy.traverser
import mypy.util
from mypy.build import build
from mypy.errors import CompileError, Errors
from mypy.find_sources import InvalidSourceList, create_source_list
from mypy.modulefinder import (
    BuildSource,
    FindModuleCache,
    ModuleNotFoundReason,
    SearchPaths,
    default_lib_path,
)
from mypy.moduleinspect import ModuleInspect
from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    ARG_STAR,
    ARG_STAR2,
    IS_ABSTRACT,
    AssignmentStmt,
    Block,
    BytesExpr,
    CallExpr,
    ClassDef,
    ComparisonExpr,
    Decorator,
    EllipsisExpr,
    Expression,
    FloatExpr,
    FuncBase,
    FuncDef,
    IfStmt,
    Import,
    ImportAll,
    ImportFrom,
    IndexExpr,
    IntExpr,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    OverloadedFuncDef,
    Statement,
    StrExpr,
    TupleExpr,
    TypeInfo,
    UnaryExpr,
)
from mypy.options import Options as MypyOptions
from mypy.stubdoc import Sig, find_unique_signatures, parse_all_signatures
from mypy.stubgenc import generate_stub_for_c_module
from mypy.stubutil import (
    CantImport,
    common_dir_prefix,
    fail_missing,
    find_module_path_and_all_py3,
    generate_guarded,
    remove_misplaced_type_comments,
    report_missing,
    walk_packages,
)
from mypy.traverser import all_yield_expressions, has_return_statement, has_yield_expression
from mypy.types import (
    OVERLOAD_NAMES,
    AnyType,
    CallableType,
    Instance,
    NoneType,
    TupleType,
    Type,
    TypeList,
    TypeStrVisitor,
    UnboundType,
    get_proper_type,
)
from mypy.visitor import NodeVisitor

TYPING_MODULE_NAMES: Final = ("typing", "typing_extensions")

# Common ways of naming package containing vendored modules.
VENDOR_PACKAGES: Final = ["packages", "vendor", "vendored", "_vendor", "_vendored_packages"]

# Avoid some file names that are unnecessary or likely to cause trouble (\n for end of path).
BLACKLIST: Final = [
    "/six.py\n",  # Likely vendored six; too dynamic for us to handle
    "/vendored/",  # Vendored packages
    "/vendor/",  # Vendored packages
    "/_vendor/",
    "/_vendored_packages/",
]

# Special-cased names that are implicitly exported from the stub (from m import y as y).
EXTRA_EXPORTED: Final = {
    "pyasn1_modules.rfc2437.univ",
    "pyasn1_modules.rfc2459.char",
    "pyasn1_modules.rfc2459.univ",
}

# These names should be omitted from generated stubs.
IGNORED_DUNDERS: Final = {
    "__all__",
    "__author__",
    "__version__",
    "__about__",
    "__copyright__",
    "__email__",
    "__license__",
    "__summary__",
    "__title__",
    "__uri__",
    "__str__",
    "__repr__",
    "__getstate__",
    "__setstate__",
    "__slots__",
}

# These methods are expected to always return a non-trivial value.
METHODS_WITH_RETURN_VALUE: Final = {
    "__ne__",
    "__eq__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__hash__",
    "__iter__",
}


class Options:
    """Represents stubgen options.

    This class is mutable to simplify testing.
    """

    def __init__(
        self,
        pyversion: Tuple[int, int],
        no_import: bool,
        doc_dir: str,
        search_path: List[str],
        interpreter: str,
        parse_only: bool,
        ignore_errors: bool,
        include_private: bool,
        output_dir: str,
        modules: List[str],
        packages: List[str],
        files: List[str],
        verbose: bool,
        quiet: bool,
        export_less: bool,
    ) -> None:
        # See parse_options for descriptions of the flags.
        self.pyversion = pyversion
        self.no_import = no_import
        self.doc_dir = doc_dir
        self.search_path = search_path
        self.interpreter = interpreter
        self.decointerpreter = interpreter
        self.parse_only = parse_only
        self.ignore_errors = ignore_errors
        self.include_private = include_private
        self.output_dir = output_dir
        self.modules = modules
        self.packages = packages
        self.files = files
        self.verbose = verbose
        self.quiet = quiet
        self.export_less = export_less


class StubSource:
    """A single source for stub: can be a Python or C module.

    A simple extension of BuildSource that also carries the AST and
    the value of __all__ detected at runtime.
    """

    def __init__(
        self, module: str, path: Optional[str] = None, runtime_all: Optional[List[str]] = None
    ) -> None:
        self.source = BuildSource(path, module, None)
        self.runtime_all = runtime_all
        self.ast: Optional[MypyFile] = None

    @property
    def module(self) -> str:
        return self.source.module

    @property
    def path(self) -> Optional[str]:
        return self.source.path


# What was generated previously in the stub file. We keep track of these to generate
# nicely formatted output (add empty line between non-empty classes, for example).
EMPTY: Final = "EMPTY"
FUNC: Final = "FUNC"
CLASS: Final = "CLASS"
EMPTY_CLASS: Final = "EMPTY_CLASS"
VAR: Final = "VAR"
NOT_IN_ALL: Final = "NOT_IN_ALL"

# Indicates that we failed to generate a reasonable output
# for a given node. These should be manually replaced by a user.

ERROR_MARKER: Final = "<ERROR>"


class AnnotationPrinter(TypeStrVisitor):
    """Visitor used to print existing annotations in a file.

    The main difference from TypeStrVisitor is a better treatment of
    unbound types.

    Notes:
    * This visitor doesn't add imports necessary for annotations, this is done separately
      by ImportTracker.
    * It can print all kinds of types, but the generated strings may not be valid (notably
      callable types) since it prints the same string that reveal_type() does.
    * For Instance types it prints the fully qualified names.
    """

    # TODO: Generate valid string representation for callable types.
    # TODO: Use short names for Instances.
    def __init__(self, stubgen: "StubGenerator") -> None:
        super().__init__()
        self.stubgen = stubgen

    def visit_any(self, t: AnyType) -> str:
        s = super().visit_any(t)
        self.stubgen.import_tracker.require_name(s)
        return s

    def visit_unbound_type(self, t: UnboundType) -> str:
        s = t.name
        self.stubgen.import_tracker.require_name(s)
        if t.args:
            s += f"[{self.args_str(t.args)}]"
        return s

    def visit_none_type(self, t: NoneType) -> str:
        return "None"

    def visit_type_list(self, t: TypeList) -> str:
        return f"[{self.list_str(t.items)}]"

    def args_str(self, args: Iterable[Type]) -> str:
        """Convert an array of arguments to strings and join the results with commas.

        The main difference from list_str is the preservation of quotes for string
        arguments
        """
        types = ["builtins.bytes", "builtins.str"]
        res = []
        for arg in args:
            arg_str = arg.accept(self)
            if isinstance(arg, UnboundType) and arg.original_str_fallback in types:
                res.append(f"'{arg_str}'")
            else:
                res.append(arg_str)
        return ", ".join(res)


class AliasPrinter(NodeVisitor[str]):
    """Visitor used to collect type aliases _and_ type variable definitions.

    Visit r.h.s of the definition to get the string representation of type alias.
    """

    def __init__(self, stubgen: "StubGenerator") -> None:
        self.stubgen = stubgen
        super().__init__()

    def visit_call_expr(self, node: CallExpr) -> str:
        # Call expressions are not usually types, but we also treat `X = TypeVar(...)` as a
        # type alias that has to be preserved (even if TypeVar is not the same as an alias)
        callee = node.callee.accept(self)
        args = []
        for name, arg, kind in zip(node.arg_names, node.args, node.arg_kinds):
            if kind == ARG_POS:
                args.append(arg.accept(self))
            elif kind == ARG_STAR:
                args.append("*" + arg.accept(self))
            elif kind == ARG_STAR2:
                args.append("**" + arg.accept(self))
            elif kind == ARG_NAMED:
                args.append(f"{name}={arg.accept(self)}")
            else:
                raise ValueError(f"Unknown argument kind {kind} in call")
        return f"{callee}({', '.join(args)})"

    def visit_name_expr(self, node: NameExpr) -> str:
        self.stubgen.import_tracker.require_name(node.name)
        return node.name

    def visit_member_expr(self, o: MemberExpr) -> str:
        node: Expression = o
        trailer = ""
        while isinstance(node, MemberExpr):
            trailer = "." + node.name + trailer
            node = node.expr
        if not isinstance(node, NameExpr):
            return ERROR_MARKER
        self.stubgen.import_tracker.require_name(node.name)
        return node.name + trailer

    def visit_str_expr(self, node: StrExpr) -> str:
        return repr(node.value)

    def visit_index_expr(self, node: IndexExpr) -> str:
        base = node.base.accept(self)
        index = node.index.accept(self)
        return f"{base}[{index}]"

    def visit_tuple_expr(self, node: TupleExpr) -> str:
        return ", ".join(n.accept(self) for n in node.items)

    def visit_list_expr(self, node: ListExpr) -> str:
        return f"[{', '.join(n.accept(self) for n in node.items)}]"

    def visit_ellipsis(self, node: EllipsisExpr) -> str:
        return "..."


class ImportTracker:
    """Record necessary imports during stub generation."""

    def __init__(self) -> None:
        # module_for['foo'] has the module name where 'foo' was imported from, or None if
        # 'foo' is a module imported directly; examples
        #     'from pkg.m import f as foo' ==> module_for['foo'] == 'pkg.m'
        #     'from m import f' ==> module_for['f'] == 'm'
        #     'import m' ==> module_for['m'] == None
        #     'import pkg.m' ==> module_for['pkg.m'] == None
        #                    ==> module_for['pkg'] == None
        self.module_for: Dict[str, Optional[str]] = {}

        # direct_imports['foo'] is the module path used when the name 'foo' was added to the
        # namespace.
        #   import foo.bar.baz  ==> direct_imports['foo'] == 'foo.bar.baz'
        #                       ==> direct_imports['foo.bar'] == 'foo.bar.baz'
        #                       ==> direct_imports['foo.bar.baz'] == 'foo.bar.baz'
        self.direct_imports: Dict[str, str] = {}

        # reverse_alias['foo'] is the name that 'foo' had originally when imported with an
        # alias; examples
        #     'import numpy as np' ==> reverse_alias['np'] == 'numpy'
        #     'import foo.bar as bar' ==> reverse_alias['bar'] == 'foo.bar'
        #     'from decimal import Decimal as D' ==> reverse_alias['D'] == 'Decimal'
        self.reverse_alias: Dict[str, str] = {}

        # required_names is the set of names that are actually used in a type annotation
        self.required_names: Set[str] = set()

        # Names that should be reexported if they come from another module
        self.reexports: Set[str] = set()

    def add_import_from(self, module: str, names: List[Tuple[str, Optional[str]]]) -> None:
        for name, alias in names:
            if alias:
                # 'from {module} import {name} as {alias}'
                self.module_for[alias] = module
                self.reverse_alias[alias] = name
            else:
                # 'from {module} import {name}'
                self.module_for[name] = module
                self.reverse_alias.pop(name, None)
            self.direct_imports.pop(alias or name, None)

    def add_import(self, module: str, alias: Optional[str] = None) -> None:
        if alias:
            # 'import {module} as {alias}'
            self.module_for[alias] = None
            self.reverse_alias[alias] = module
        else:
            # 'import {module}'
            name = module
            # add module and its parent packages
            while name:
                self.module_for[name] = None
                self.direct_imports[name] = module
                self.reverse_alias.pop(name, None)
                name = name.rpartition(".")[0]

    def require_name(self, name: str) -> None:
        self.required_names.add(name.split(".")[0])

    def reexport(self, name: str) -> None:
        """Mark a given non qualified name as needed in __all__.

        This means that in case it comes from a module, it should be
        imported with an alias even is the alias is the same as the name.
        """
        self.require_name(name)
        self.reexports.add(name)

    def import_lines(self) -> List[str]:
        """The list of required import lines (as strings with python code)."""
        result = []

        # To summarize multiple names imported from a same module, we collect those
        # in the `module_map` dictionary, mapping a module path to the list of names that should
        # be imported from it. the names can also be alias in the form 'original as alias'
        module_map: Mapping[str, List[str]] = defaultdict(list)

        for name in sorted(self.required_names):
            # If we haven't seen this name in an import statement, ignore it
            if name not in self.module_for:
                continue

            m = self.module_for[name]
            if m is not None:
                # This name was found in a from ... import ...
                # Collect the name in the module_map
                if name in self.reverse_alias:
                    name = f"{self.reverse_alias[name]} as {name}"
                elif name in self.reexports:
                    name = f"{name} as {name}"
                module_map[m].append(name)
            else:
                # This name was found in an import ...
                # We can already generate the import line
                if name in self.reverse_alias:
                    source = self.reverse_alias[name]
                    result.append(f"import {source} as {name}\n")
                elif name in self.reexports:
                    assert "." not in name  # Because reexports only has nonqualified names
                    result.append(f"import {name} as {name}\n")
                else:
                    result.append(f"import {self.direct_imports[name]}\n")

        # Now generate all the from ... import ... lines collected in module_map
        for module, names in sorted(module_map.items()):
            result.append(f"from {module} import {', '.join(sorted(names))}\n")
        return result


def find_defined_names(file: MypyFile) -> Set[str]:
    finder = DefinitionFinder()
    file.accept(finder)
    return finder.names


class DefinitionFinder(mypy.traverser.TraverserVisitor):
    """Find names of things defined at the top level of a module."""

    # TODO: Assignment statements etc.

    def __init__(self) -> None:
        # Short names of things defined at the top level.
        self.names: Set[str] = set()

    def visit_class_def(self, o: ClassDef) -> None:
        # Don't recurse into classes, as we only keep track of top-level definitions.
        self.names.add(o.name)

    def visit_func_def(self, o: FuncDef) -> None:
        # Don't recurse, as we only keep track of top-level definitions.
        self.names.add(o.name)


def find_referenced_names(file: MypyFile) -> Set[str]:
    finder = ReferenceFinder()
    file.accept(finder)
    return finder.refs


class ReferenceFinder(mypy.mixedtraverser.MixedTraverserVisitor):
    """Find all name references (both local and global)."""

    # TODO: Filter out local variable and class attribute references

    def __init__(self) -> None:
        # Short names of things defined at the top level.
        self.refs: Set[str] = set()

    def visit_block(self, block: Block) -> None:
        if not block.is_unreachable:
            super().visit_block(block)

    def visit_name_expr(self, e: NameExpr) -> None:
        self.refs.add(e.name)

    def visit_instance(self, t: Instance) -> None:
        self.add_ref(t.type.fullname)
        super().visit_instance(t)

    def visit_unbound_type(self, t: UnboundType) -> None:
        if t.name:
            self.add_ref(t.name)

    def visit_tuple_type(self, t: TupleType) -> None:
        # Ignore fallback
        for item in t.items:
            item.accept(self)

    def visit_callable_type(self, t: CallableType) -> None:
        # Ignore fallback
        for arg in t.arg_types:
            arg.accept(self)
        t.ret_type.accept(self)

    def add_ref(self, fullname: str) -> None:
        self.refs.add(fullname.split(".")[-1])


class StubGenerator(mypy.traverser.TraverserVisitor):
    """Generate stub text from a mypy AST."""

    def __init__(
        self,
        _all_: Optional[List[str]],
        include_private: bool = False,
        analyzed: bool = False,
        export_less: bool = False,
    ) -> None:
        # Best known value of __all__.
        self._all_ = _all_
        self._output: List[str] = []
        self._decorators: List[str] = []
        self._import_lines: List[str] = []
        # Current indent level (indent is hardcoded to 4 spaces).
        self._indent = ""
        # Stack of defined variables (per scope).
        self._vars: List[List[str]] = [[]]
        # What was generated previously in the stub file.
        self._state = EMPTY
        self._toplevel_names: List[str] = []
        self._include_private = include_private
        self.import_tracker = ImportTracker()
        # Was the tree semantically analysed before?
        self.analyzed = analyzed
        # Disable implicit exports of package-internal imports?
        self.export_less = export_less
        # Add imports that could be implicitly generated
        self.import_tracker.add_import_from("typing", [("NamedTuple", None)])
        # Names in __all__ are required
        for name in _all_ or ():
            if name not in IGNORED_DUNDERS:
                self.import_tracker.reexport(name)
        self.defined_names: Set[str] = set()
        # Short names of methods defined in the body of the current class
        self.method_names: Set[str] = set()

    def visit_mypy_file(self, o: MypyFile) -> None:
        self.module = o.fullname  # Current module being processed
        self.path = o.path
        self.defined_names = find_defined_names(o)
        self.referenced_names = find_referenced_names(o)
        known_imports = {
            "_typeshed": ["Incomplete"],
            "typing": ["Any", "TypeVar"],
            "collections.abc": ["Generator"],
        }
        for pkg, imports in known_imports.items():
            for t in imports:
                if t not in self.defined_names:
                    alias = None
                else:
                    alias = "_" + t
                self.import_tracker.add_import_from(pkg, [(t, alias)])
        super().visit_mypy_file(o)
        undefined_names = [name for name in self._all_ or [] if name not in self._toplevel_names]
        if undefined_names:
            if self._state != EMPTY:
                self.add("\n")
            self.add("# Names in __all__ with no definition:\n")
            for name in sorted(undefined_names):
                self.add(f"#   {name}\n")

    def visit_overloaded_func_def(self, o: OverloadedFuncDef) -> None:
        """@property with setters and getters, or @overload chain"""
        overload_chain = False
        for item in o.items:
            if not isinstance(item, Decorator):
                continue

            if self.is_private_name(item.func.name, item.func.fullname):
                continue

            is_abstract, is_overload = self.process_decorator(item)

            if not overload_chain:
                self.visit_func_def(item.func, is_abstract=is_abstract, is_overload=is_overload)
                if is_overload:
                    overload_chain = True
            elif overload_chain and is_overload:
                self.visit_func_def(item.func, is_abstract=is_abstract, is_overload=is_overload)
            else:
                # skip the overload implementation and clear the decorator we just processed
                self.clear_decorators()

    def visit_func_def(
        self, o: FuncDef, is_abstract: bool = False, is_overload: bool = False
    ) -> None:
        if (
            self.is_private_name(o.name, o.fullname)
            or self.is_not_in_all(o.name)
            or (self.is_recorded_name(o.name) and not is_overload)
        ):
            self.clear_decorators()
            return
        if not self._indent and self._state not in (EMPTY, FUNC) and not o.is_awaitable_coroutine:
            self.add("\n")
        if not self.is_top_level():
            self_inits = find_self_initializers(o)
            for init, value in self_inits:
                if init in self.method_names:
                    # Can't have both an attribute and a method/property with the same name.
                    continue
                init_code = self.get_init(init, value)
                if init_code:
                    self.add(init_code)
        # dump decorators, just before "def ..."
        for s in self._decorators:
            self.add(s)
        self.clear_decorators()
        self.add(f"{self._indent}{'async ' if o.is_coroutine else ''}def {o.name}(")
        self.record_name(o.name)
        args: List[str] = []
        for i, arg_ in enumerate(o.arguments):
            var = arg_.variable
            kind = arg_.kind
            name = var.name
            annotated_type = (
                o.unanalyzed_type.arg_types[i]
                if isinstance(o.unanalyzed_type, CallableType)
                else None
            )
            # I think the name check is incorrect: there are libraries which
            # name their 0th argument other than self/cls
            is_self_arg = i == 0 and name == "self"
            is_cls_arg = i == 0 and name == "cls"
            annotation = ""
            if annotated_type and not is_self_arg and not is_cls_arg:
                # Luckily, an argument explicitly annotated with "Any" has
                # type "UnboundType" and will not match.
                if not isinstance(get_proper_type(annotated_type), AnyType):
                    annotation = f": {self.print_annotation(annotated_type)}"

            if kind.is_named() and not any(arg.startswith("*") for arg in args):
                args.append("*")

            if arg_.initializer:
                if not annotation:
                    typename = self.get_str_type_of_node(arg_.initializer, True, False)
                    if typename == "":
                        annotation = "=..."
                    else:
                        annotation = f": {typename} = ..."
                else:
                    annotation += " = ..."
                arg = name + annotation
            elif kind == ARG_STAR:
                arg = f"*{name}{annotation}"
            elif kind == ARG_STAR2:
                arg = f"**{name}{annotation}"
            else:
                arg = name + annotation
            args.append(arg)
        retname = None
        if o.name != "__init__" and isinstance(o.unanalyzed_type, CallableType):
            if isinstance(get_proper_type(o.unanalyzed_type.ret_type), AnyType):
                # Luckily, a return type explicitly annotated with "Any" has
                # type "UnboundType" and will enter the else branch.
                retname = None  # implicit Any
            else:
                retname = self.print_annotation(o.unanalyzed_type.ret_type)
        elif isinstance(o, FuncDef) and (
            o.abstract_status == IS_ABSTRACT or o.name in METHODS_WITH_RETURN_VALUE
        ):
            # Always assume abstract methods return Any unless explicitly annotated. Also
            # some dunder methods should not have a None return type.
            retname = None  # implicit Any
        elif has_yield_expression(o):
            self.add_abc_import("Generator")
            yield_name = "None"
            send_name = "None"
            return_name = "None"
            for expr, in_assignment in all_yield_expressions(o):
                if expr.expr is not None and not self.is_none_expr(expr.expr):
                    self.add_typing_import("Incomplete")
                    yield_name = "Incomplete"
                if in_assignment:
                    self.add_typing_import("Incomplete")
                    send_name = "Incomplete"
            if has_return_statement(o):
                self.add_typing_import("Incomplete")
                return_name = "Incomplete"
            generator_name = self.typing_name("Generator")
            retname = f"{generator_name}[{yield_name}, {send_name}, {return_name}]"
        elif not has_return_statement(o) and not is_abstract:
            retname = "None"
        retfield = ""
        if retname is not None:
            retfield = " -> " + retname

        self.add(", ".join(args))
        self.add(f"){retfield}: ...\n")
        self._state = FUNC

    def is_none_expr(self, expr: Expression) -> bool:
        return isinstance(expr, NameExpr) and expr.name == "None"

    def visit_decorator(self, o: Decorator) -> None:
        if self.is_private_name(o.func.name, o.func.fullname):
            return

        is_abstract, _ = self.process_decorator(o)
        self.visit_func_def(o.func, is_abstract=is_abstract)

    def process_decorator(self, o: Decorator) -> Tuple[bool, bool]:
        """Process a series of decorators.

        Only preserve certain special decorators such as @abstractmethod.

        Return a pair of booleans:
        - True if any of the decorators makes a method abstract.
        - True if any of the decorators is typing.overload.
        """
        is_abstract = False
        is_overload = False
        for decorator in o.original_decorators:
            if isinstance(decorator, NameExpr):
                i_is_abstract, i_is_overload = self.process_name_expr_decorator(decorator, o)
                is_abstract = is_abstract or i_is_abstract
                is_overload = is_overload or i_is_overload
            elif isinstance(decorator, MemberExpr):
                i_is_abstract, i_is_overload = self.process_member_expr_decorator(decorator, o)
                is_abstract = is_abstract or i_is_abstract
                is_overload = is_overload or i_is_overload
        return is_abstract, is_overload

    def process_name_expr_decorator(self, expr: NameExpr, context: Decorator) -> Tuple[bool, bool]:
        """Process a function decorator of form @foo.

        Only preserve certain special decorators such as @abstractmethod.

        Return a pair of booleans:
        - True if the decorator makes a method abstract.
        - True if the decorator is typing.overload.
        """
        is_abstract = False
        is_overload = False
        name = expr.name
        if name in ("property", "staticmethod", "classmethod"):
            self.add_decorator(name)
        elif self.import_tracker.module_for.get(name) in (
            "asyncio",
            "asyncio.coroutines",
            "types",
        ):
            self.add_coroutine_decorator(context.func, name, name)
        elif self.refers_to_fullname(name, "abc.abstractmethod"):
            self.add_decorator(name)
            self.import_tracker.require_name(name)
            is_abstract = True
        elif self.refers_to_fullname(name, "abc.abstractproperty"):
            self.add_decorator("property")
            self.add_decorator("abc.abstractmethod")
            is_abstract = True
        elif self.refers_to_fullname(name, OVERLOAD_NAMES):
            self.add_decorator(name)
            self.add_typing_import("overload")
            is_overload = True
        return is_abstract, is_overload

    def refers_to_fullname(self, name: str, fullname: Union[str, Tuple[str, ...]]) -> bool:
        if isinstance(fullname, tuple):
            return any(self.refers_to_fullname(name, fname) for fname in fullname)
        module, short = fullname.rsplit(".", 1)
        return self.import_tracker.module_for.get(name) == module and (
            name == short or self.import_tracker.reverse_alias.get(name) == short
        )

    def process_member_expr_decorator(
        self, expr: MemberExpr, context: Decorator
    ) -> Tuple[bool, bool]:
        """Process a function decorator of form @foo.bar.

        Only preserve certain special decorators such as @abstractmethod.

        Return a pair of booleans:
        - True if the decorator makes a method abstract.
        - True if the decorator is typing.overload.
        """
        is_abstract = False
        is_overload = False
        if expr.name == "setter" and isinstance(expr.expr, NameExpr):
            self.add_decorator(f"{expr.expr.name}.setter")
        elif (
            isinstance(expr.expr, NameExpr)
            and (
                expr.expr.name == "abc"
                or self.import_tracker.reverse_alias.get(expr.expr.name) == "abc"
            )
            and expr.name in ("abstractmethod", "abstractproperty")
        ):
            if expr.name == "abstractproperty":
                self.import_tracker.require_name(expr.expr.name)
                self.add_decorator("%s" % ("property"))
                self.add_decorator("{}.{}".format(expr.expr.name, "abstractmethod"))
            else:
                self.import_tracker.require_name(expr.expr.name)
                self.add_decorator(f"{expr.expr.name}.{expr.name}")
            is_abstract = True
        elif expr.name == "coroutine":
            if (
                isinstance(expr.expr, MemberExpr)
                and expr.expr.name == "coroutines"
                and isinstance(expr.expr.expr, NameExpr)
                and (
                    expr.expr.expr.name == "asyncio"
                    or self.import_tracker.reverse_alias.get(expr.expr.expr.name) == "asyncio"
                )
            ):
                self.add_coroutine_decorator(
                    context.func,
                    "%s.coroutines.coroutine" % (expr.expr.expr.name,),
                    expr.expr.expr.name,
                )
            elif isinstance(expr.expr, NameExpr) and (
                expr.expr.name in ("asyncio", "types")
                or self.import_tracker.reverse_alias.get(expr.expr.name)
                in ("asyncio", "asyncio.coroutines", "types")
            ):
                self.add_coroutine_decorator(
                    context.func, expr.expr.name + ".coroutine", expr.expr.name
                )
        elif (
            isinstance(expr.expr, NameExpr)
            and (
                expr.expr.name in TYPING_MODULE_NAMES
                or self.import_tracker.reverse_alias.get(expr.expr.name) in TYPING_MODULE_NAMES
            )
            and expr.name == "overload"
        ):
            self.import_tracker.require_name(expr.expr.name)
            self.add_decorator(f"{expr.expr.name}.overload")
            is_overload = True
        return is_abstract, is_overload

    def visit_class_def(self, o: ClassDef) -> None:
        self.method_names = find_method_names(o.defs.body)
        sep: Optional[int] = None
        if not self._indent and self._state != EMPTY:
            sep = len(self._output)
            self.add("\n")
        self.add(f"{self._indent}class {o.name}")
        self.record_name(o.name)
        base_types = self.get_base_types(o)
        if base_types:
            for base in base_types:
                self.import_tracker.require_name(base)
        if isinstance(o.metaclass, (NameExpr, MemberExpr)):
            meta = o.metaclass.accept(AliasPrinter(self))
            base_types.append("metaclass=" + meta)
        elif self.analyzed and o.info.is_protocol:
            type_str = "Protocol"
            if o.info.type_vars:
                type_str += f'[{", ".join(o.info.type_vars)}]'
            base_types.append(type_str)
            self.add_typing_import("Protocol")
        elif self.analyzed and o.info.is_abstract:
            base_types.append("metaclass=abc.ABCMeta")
            self.import_tracker.add_import("abc")
            self.import_tracker.require_name("abc")
        if base_types:
            self.add(f"({', '.join(base_types)})")
        self.add(":\n")
        n = len(self._output)
        self._indent += "    "
        self._vars.append([])
        super().visit_class_def(o)
        self._indent = self._indent[:-4]
        self._vars.pop()
        self._vars[-1].append(o.name)
        if len(self._output) == n:
            if self._state == EMPTY_CLASS and sep is not None:
                self._output[sep] = ""
            self._output[-1] = self._output[-1][:-1] + " ...\n"
            self._state = EMPTY_CLASS
        else:
            self._state = CLASS
        self.method_names = set()

    def get_base_types(self, cdef: ClassDef) -> List[str]:
        """Get list of base classes for a class."""
        base_types: List[str] = []
        for base in cdef.base_type_exprs:
            if isinstance(base, NameExpr):
                if base.name != "object":
                    base_types.append(base.name)
            elif isinstance(base, MemberExpr):
                modname = get_qualified_name(base.expr)
                base_types.append(f"{modname}.{base.name}")
            elif isinstance(base, IndexExpr):
                p = AliasPrinter(self)
                base_types.append(base.accept(p))
        return base_types

    def visit_block(self, o: Block) -> None:
        # Unreachable statements may be partially uninitialized and that may
        # cause trouble.
        if not o.is_unreachable:
            super().visit_block(o)

    def visit_assignment_stmt(self, o: AssignmentStmt) -> None:
        foundl = []

        for lvalue in o.lvalues:
            if isinstance(lvalue, NameExpr) and self.is_namedtuple(o.rvalue):
                assert isinstance(o.rvalue, CallExpr)
                self.process_namedtuple(lvalue, o.rvalue)
                continue
            if (
                self.is_top_level()
                and isinstance(lvalue, NameExpr)
                and not self.is_private_name(lvalue.name)
                and
                # it is never an alias with explicit annotation
                not o.unanalyzed_type
                and self.is_alias_expression(o.rvalue)
            ):
                self.process_typealias(lvalue, o.rvalue)
                continue
            if isinstance(lvalue, TupleExpr) or isinstance(lvalue, ListExpr):
                items = lvalue.items
                if isinstance(o.unanalyzed_type, TupleType):  # type: ignore
                    annotations: Iterable[Optional[Type]] = o.unanalyzed_type.items
                else:
                    annotations = [None] * len(items)
            else:
                items = [lvalue]
                annotations = [o.unanalyzed_type]
            sep = False
            found = False
            for item, annotation in zip(items, annotations):
                if isinstance(item, NameExpr):
                    init = self.get_init(item.name, o.rvalue, annotation)
                    if init:
                        found = True
                        if not sep and not self._indent and self._state not in (EMPTY, VAR):
                            init = "\n" + init
                            sep = True
                        self.add(init)
                        self.record_name(item.name)
            foundl.append(found)

        if all(foundl):
            self._state = VAR

    def is_namedtuple(self, expr: Expression) -> bool:
        if not isinstance(expr, CallExpr):
            return False
        callee = expr.callee
        return (isinstance(callee, NameExpr) and callee.name.endswith("namedtuple")) or (
            isinstance(callee, MemberExpr) and callee.name == "namedtuple"
        )

    def process_namedtuple(self, lvalue: NameExpr, rvalue: CallExpr) -> None:
        if self._state != EMPTY:
            self.add("\n")
        if isinstance(rvalue.args[1], StrExpr):
            items = rvalue.args[1].value.replace(",", " ").split()
        elif isinstance(rvalue.args[1], (ListExpr, TupleExpr)):
            list_items = cast(List[StrExpr], rvalue.args[1].items)
            items = [item.value for item in list_items]
        else:
            self.add(f"{self._indent}{lvalue.name}: Incomplete")
            self.import_tracker.require_name("Incomplete")
            return
        self.import_tracker.require_name("NamedTuple")
        self.add(f"{self._indent}class {lvalue.name}(NamedTuple):")
        if len(items) == 0:
            self.add(" ...\n")
        else:
            self.import_tracker.require_name("Incomplete")
            self.add("\n")
            for item in items:
                self.add(f"{self._indent}    {item}: Incomplete\n")
        self._state = CLASS

    def is_alias_expression(self, expr: Expression, top_level: bool = True) -> bool:
        """Return True for things that look like target for an alias.

        Used to know if assignments look like type aliases, function alias,
        or module alias.
        """
        # Assignment of TypeVar(...) are passed through
        if (
            isinstance(expr, CallExpr)
            and isinstance(expr.callee, NameExpr)
            and expr.callee.name == "TypeVar"
        ):
            return True
        elif isinstance(expr, EllipsisExpr):
            return not top_level
        elif isinstance(expr, NameExpr):
            if expr.name in ("True", "False"):
                return False
            elif expr.name == "None":
                return not top_level
            else:
                return not self.is_private_name(expr.name)
        elif isinstance(expr, MemberExpr) and self.analyzed:
            # Also add function and module aliases.
            return (
                top_level
                and isinstance(expr.node, (FuncDef, Decorator, MypyFile))
                or isinstance(expr.node, TypeInfo)
            ) and not self.is_private_member(expr.node.fullname)
        elif (
            isinstance(expr, IndexExpr)
            and isinstance(expr.base, NameExpr)
            and not self.is_private_name(expr.base.name)
        ):
            if isinstance(expr.index, TupleExpr):
                indices = expr.index.items
            else:
                indices = [expr.index]
            if expr.base.name == "Callable" and len(indices) == 2:
                args, ret = indices
                if isinstance(args, EllipsisExpr):
                    indices = [ret]
                elif isinstance(args, ListExpr):
                    indices = args.items + [ret]
                else:
                    return False
            return all(self.is_alias_expression(i, top_level=False) for i in indices)
        else:
            return False

    def process_typealias(self, lvalue: NameExpr, rvalue: Expression) -> None:
        p = AliasPrinter(self)
        self.add(f"{lvalue.name} = {rvalue.accept(p)}\n")
        self.record_name(lvalue.name)
        self._vars[-1].append(lvalue.name)

    def visit_if_stmt(self, o: IfStmt) -> None:
        # Ignore if __name__ == '__main__'.
        expr = o.expr[0]
        if (
            isinstance(expr, ComparisonExpr)
            and isinstance(expr.operands[0], NameExpr)
            and isinstance(expr.operands[1], StrExpr)
            and expr.operands[0].name == "__name__"
            and "__main__" in expr.operands[1].value
        ):
            return
        super().visit_if_stmt(o)

    def visit_import_all(self, o: ImportAll) -> None:
        self.add_import_line(f"from {'.' * o.relative}{o.id} import *\n")

    def visit_import_from(self, o: ImportFrom) -> None:
        exported_names: Set[str] = set()
        import_names = []
        module, relative = translate_module_name(o.id, o.relative)
        if self.module:
            full_module, ok = mypy.util.correct_relative_import(
                self.module, relative, module, self.path.endswith(".__init__.py")
            )
            if not ok:
                full_module = module
        else:
            full_module = module
        if module == "__future__":
            return  # Not preserved
        for name, as_name in o.names:
            if name == "six":
                # Vendored six -- translate into plain 'import six'.
                self.visit_import(Import([("six", None)]))
                continue
            exported = False
            if as_name is None and self.module and (self.module + "." + name) in EXTRA_EXPORTED:
                # Special case certain names that should be exported, against our general rules.
                exported = True
            is_private = self.is_private_name(name, full_module + "." + name)
            if (
                as_name is None
                and name not in self.referenced_names
                and (not self._all_ or name in IGNORED_DUNDERS)
                and not is_private
                and module not in ("abc", "asyncio") + TYPING_MODULE_NAMES
            ):
                # An imported name that is never referenced in the module is assumed to be
                # exported, unless there is an explicit __all__. Note that we need to special
                # case 'abc' since some references are deleted during semantic analysis.
                exported = True
            top_level = full_module.split(".")[0]
            if (
                as_name is None
                and not self.export_less
                and (not self._all_ or name in IGNORED_DUNDERS)
                and self.module
                and not is_private
                and top_level in (self.module.split(".")[0], "_" + self.module.split(".")[0])
            ):
                # Export imports from the same package, since we can't reliably tell whether they
                # are part of the public API.
                exported = True
            if exported:
                self.import_tracker.reexport(name)
                as_name = name
            import_names.append((name, as_name))
        self.import_tracker.add_import_from("." * relative + module, import_names)
        self._vars[-1].extend(alias or name for name, alias in import_names)
        for name, alias in import_names:
            self.record_name(alias or name)

        if self._all_:
            # Include "import from"s that import names defined in __all__.
            names = [
                name
                for name, alias in o.names
                if name in self._all_ and alias is None and name not in IGNORED_DUNDERS
            ]
            exported_names.update(names)

    def visit_import(self, o: Import) -> None:
        for id, as_id in o.ids:
            self.import_tracker.add_import(id, as_id)
            if as_id is None:
                target_name = id.split(".")[0]
            else:
                target_name = as_id
            self._vars[-1].append(target_name)
            self.record_name(target_name)

    def get_init(
        self, lvalue: str, rvalue: Expression, annotation: Optional[Type] = None
    ) -> Optional[str]:
        """Return initializer for a variable.

        Return None if we've generated one already or if the variable is internal.
        """
        if lvalue in self._vars[-1]:
            # We've generated an initializer already for this variable.
            return None
        # TODO: Only do this at module top level.
        if self.is_private_name(lvalue) or self.is_not_in_all(lvalue):
            return None
        self._vars[-1].append(lvalue)
        if annotation is not None:
            typename = self.print_annotation(annotation)
            if (
                isinstance(annotation, UnboundType)
                and not annotation.args
                and annotation.name == "Final"
                and self.import_tracker.module_for.get("Final") in TYPING_MODULE_NAMES
            ):
                # Final without type argument is invalid in stubs.
                final_arg = self.get_str_type_of_node(rvalue)
                typename += f"[{final_arg}]"
        else:
            typename = self.get_str_type_of_node(rvalue)
        return f"{self._indent}{lvalue}: {typename}\n"

    def add(self, string: str) -> None:
        """Add text to generated stub."""
        self._output.append(string)

    def add_decorator(self, name: str) -> None:
        if not self._indent and self._state not in (EMPTY, FUNC):
            self._decorators.append("\n")
        self._decorators.append(f"{self._indent}@{name}\n")

    def clear_decorators(self) -> None:
        self._decorators.clear()

    def typing_name(self, name: str) -> str:
        if name in self.defined_names:
            # Avoid name clash between name from typing and a name defined in stub.
            return "_" + name
        else:
            return name

    def add_typing_import(self, name: str) -> None:
        """Add a name to be imported from typing, unless it's imported already.

        The import will be internal to the stub.
        """
        name = self.typing_name(name)
        self.import_tracker.require_name(name)

    def add_abc_import(self, name: str) -> None:
        """Add a name to be imported from collections.abc, unless it's imported already.

        The import will be internal to the stub.
        """
        name = self.typing_name(name)
        self.import_tracker.require_name(name)

    def add_import_line(self, line: str) -> None:
        """Add a line of text to the import section, unless it's already there."""
        if line not in self._import_lines:
            self._import_lines.append(line)

    def add_coroutine_decorator(self, func: FuncDef, name: str, require_name: str) -> None:
        func.is_awaitable_coroutine = True
        self.add_decorator(name)
        self.import_tracker.require_name(require_name)

    def output(self) -> str:
        """Return the text for the stub."""
        imports = ""
        if self._import_lines:
            imports += "".join(self._import_lines)
        imports += "".join(self.import_tracker.import_lines())
        if imports and self._output:
            imports += "\n"
        return imports + "".join(self._output)

    def is_not_in_all(self, name: str) -> bool:
        if self.is_private_name(name):
            return False
        if self._all_:
            return self.is_top_level() and name not in self._all_
        return False

    def is_private_name(self, name: str, fullname: Optional[str] = None) -> bool:
        if self._include_private:
            return False
        if fullname in EXTRA_EXPORTED:
            return False
        return name.startswith("_") and (not name.endswith("__") or name in IGNORED_DUNDERS)

    def is_private_member(self, fullname: str) -> bool:
        parts = fullname.split(".")
        for part in parts:
            if self.is_private_name(part):
                return True
        return False

    def get_str_type_of_node(
        self, rvalue: Expression, can_infer_optional: bool = False, can_be_any: bool = True
    ) -> str:
        if isinstance(rvalue, IntExpr):
            return "int"
        if isinstance(rvalue, StrExpr):
            return "str"
        if isinstance(rvalue, BytesExpr):
            return "bytes"
        if isinstance(rvalue, FloatExpr):
            return "float"
        if isinstance(rvalue, UnaryExpr) and isinstance(rvalue.expr, IntExpr):
            return "int"
        if isinstance(rvalue, NameExpr) and rvalue.name in ("True", "False"):
            return "bool"
        if can_infer_optional and isinstance(rvalue, NameExpr) and rvalue.name == "None":
            self.add_typing_import("Incomplete")
            return f"{self.typing_name('Incomplete')} | None"
        if can_be_any:
            self.add_typing_import("Incomplete")
            return self.typing_name("Incomplete")
        else:
            return ""

    def print_annotation(self, t: Type) -> str:
        printer = AnnotationPrinter(self)
        return t.accept(printer)

    def is_top_level(self) -> bool:
        """Are we processing the top level of a file?"""
        return self._indent == ""

    def record_name(self, name: str) -> None:
        """Mark a name as defined.

        This only does anything if at the top level of a module.
        """
        if self.is_top_level():
            self._toplevel_names.append(name)

    def is_recorded_name(self, name: str) -> bool:
        """Has this name been recorded previously?"""
        return self.is_top_level() and name in self._toplevel_names


def find_method_names(defs: List[Statement]) -> Set[str]:
    # TODO: Traverse into nested definitions
    result = set()
    for defn in defs:
        if isinstance(defn, FuncDef):
            result.add(defn.name)
        elif isinstance(defn, Decorator):
            result.add(defn.func.name)
        elif isinstance(defn, OverloadedFuncDef):
            for item in defn.items:
                result.update(find_method_names([item]))
    return result


class SelfTraverser(mypy.traverser.TraverserVisitor):
    def __init__(self) -> None:
        self.results: List[Tuple[str, Expression]] = []

    def visit_assignment_stmt(self, o: AssignmentStmt) -> None:
        lvalue = o.lvalues[0]
        if (
            isinstance(lvalue, MemberExpr)
            and isinstance(lvalue.expr, NameExpr)
            and lvalue.expr.name == "self"
        ):
            self.results.append((lvalue.name, o.rvalue))


def find_self_initializers(fdef: FuncBase) -> List[Tuple[str, Expression]]:
    """Find attribute initializers in a method.

    Return a list of pairs (attribute name, r.h.s. expression).
    """
    traverser = SelfTraverser()
    fdef.accept(traverser)
    return traverser.results


def get_qualified_name(o: Expression) -> str:
    if isinstance(o, NameExpr):
        return o.name
    elif isinstance(o, MemberExpr):
        return f"{get_qualified_name(o.expr)}.{o.name}"
    else:
        return ERROR_MARKER


def remove_blacklisted_modules(modules: List[StubSource]) -> List[StubSource]:
    return [
        module for module in modules if module.path is None or not is_blacklisted_path(module.path)
    ]


def is_blacklisted_path(path: str) -> bool:
    return any(substr in (normalize_path_separators(path) + "\n") for substr in BLACKLIST)


def normalize_path_separators(path: str) -> str:
    if sys.platform == "win32":
        return path.replace("\\", "/")
    return path


def collect_build_targets(
    options: Options, mypy_opts: MypyOptions
) -> Tuple[List[StubSource], List[StubSource]]:
    """Collect files for which we need to generate stubs.

    Return list of Python modules and C modules.
    """
    if options.packages or options.modules:
        if options.no_import:
            py_modules = find_module_paths_using_search(
                options.modules, options.packages, options.search_path, options.pyversion
            )
            c_modules: List[StubSource] = []
        else:
            # Using imports is the default, since we can also find C modules.
            py_modules, c_modules = find_module_paths_using_imports(
                options.modules, options.packages, options.verbose, options.quiet
            )
    else:
        # Use mypy native source collection for files and directories.
        try:
            source_list = create_source_list(options.files, mypy_opts)
        except InvalidSourceList as e:
            raise SystemExit(str(e)) from e
        py_modules = [StubSource(m.module, m.path) for m in source_list]
        c_modules = []

    py_modules = remove_blacklisted_modules(py_modules)

    return py_modules, c_modules


def find_module_paths_using_imports(
    modules: List[str], packages: List[str], verbose: bool, quiet: bool
) -> Tuple[List[StubSource], List[StubSource]]:
    """Find path and runtime value of __all__ (if possible) for modules and packages.

    This function uses runtime Python imports to get the information.
    """
    with ModuleInspect() as inspect:
        py_modules: List[StubSource] = []
        c_modules: List[StubSource] = []
        found = list(walk_packages(inspect, packages, verbose))
        modules = modules + found
        modules = [
            mod for mod in modules if not is_non_library_module(mod)
        ]  # We don't want to run any tests or scripts
        for mod in modules:
            try:
                result = find_module_path_and_all_py3(inspect, mod, verbose)
            except CantImport as e:
                tb = traceback.format_exc()
                if verbose:
                    sys.stdout.write(tb)
                if not quiet:
                    report_missing(mod, e.message, tb)
                continue
            if not result:
                c_modules.append(StubSource(mod))
            else:
                path, runtime_all = result
                py_modules.append(StubSource(mod, path, runtime_all))
        return py_modules, c_modules


def is_non_library_module(module: str) -> bool:
    """Does module look like a test module or a script?"""
    if module.endswith(
        (
            ".tests",
            ".test",
            ".testing",
            "_tests",
            "_test_suite",
            "test_util",
            "test_utils",
            "test_base",
            ".__main__",
            ".conftest",  # Used by pytest
            ".setup",  # Typically an install script
        )
    ):
        return True
    if module.split(".")[-1].startswith("test_"):
        return True
    if (
        ".tests." in module
        or ".test." in module
        or ".testing." in module
        or ".SelfTest." in module
    ):
        return True
    return False


def translate_module_name(module: str, relative: int) -> Tuple[str, int]:
    for pkg in VENDOR_PACKAGES:
        for alt in "six.moves", "six":
            substr = f"{pkg}.{alt}"
            if module.endswith("." + substr) or (module == substr and relative):
                return alt, 0
            if "." + substr + "." in module:
                return alt + "." + module.partition("." + substr + ".")[2], 0
    return module, relative


def find_module_paths_using_search(
    modules: List[str], packages: List[str], search_path: List[str], pyversion: Tuple[int, int]
) -> List[StubSource]:
    """Find sources for modules and packages requested.

    This function just looks for source files at the file system level.
    This is used if user passes --no-import, and will not find C modules.
    Exit if some of the modules or packages can't be found.
    """
    result: List[StubSource] = []
    typeshed_path = default_lib_path(mypy.build.default_data_dir(), pyversion, None)
    search_paths = SearchPaths((".",) + tuple(search_path), (), (), tuple(typeshed_path))
    cache = FindModuleCache(search_paths, fscache=None, options=None)
    for module in modules:
        m_result = cache.find_module(module)
        if isinstance(m_result, ModuleNotFoundReason):
            fail_missing(module, m_result)
            module_path = None
        else:
            module_path = m_result
        result.append(StubSource(module, module_path))
    for package in packages:
        p_result = cache.find_modules_recursive(package)
        if p_result:
            fail_missing(package, ModuleNotFoundReason.NOT_FOUND)
        sources = [StubSource(m.module, m.path) for m in p_result]
        result.extend(sources)

    result = [m for m in result if not is_non_library_module(m.module)]

    return result


def mypy_options(stubgen_options: Options) -> MypyOptions:
    """Generate mypy options using the flag passed by user."""
    options = MypyOptions()
    options.follow_imports = "skip"
    options.incremental = False
    options.ignore_errors = True
    options.semantic_analysis_only = True
    options.python_version = stubgen_options.pyversion
    options.show_traceback = True
    options.transform_source = remove_misplaced_type_comments
    return options


def parse_source_file(mod: StubSource, mypy_options: MypyOptions) -> None:
    """Parse a source file.

    On success, store AST in the corresponding attribute of the stub source.
    If there are syntax errors, print them and exit.
    """
    assert mod.path is not None, "Not found module was not skipped"
    with open(mod.path, "rb") as f:
        data = f.read()
    source = mypy.util.decode_python_encoding(data)
    errors = Errors()
    mod.ast = mypy.parse.parse(
        source, fnam=mod.path, module=mod.module, errors=errors, options=mypy_options
    )
    mod.ast._fullname = mod.module
    if errors.is_blockers():
        # Syntax error!
        for m in errors.new_messages():
            sys.stderr.write(f"{m}\n")
        sys.exit(1)


def generate_asts_for_modules(
    py_modules: List[StubSource], parse_only: bool, mypy_options: MypyOptions, verbose: bool
) -> None:
    """Use mypy to parse (and optionally analyze) source files."""
    if not py_modules:
        return  # Nothing to do here, but there may be C modules
    if verbose:
        print(f"Processing {len(py_modules)} files...")
    if parse_only:
        for mod in py_modules:
            parse_source_file(mod, mypy_options)
        return
    # Perform full semantic analysis of the source set.
    try:
        res = build([module.source for module in py_modules], mypy_options)
    except CompileError as e:
        raise SystemExit(f"Critical error during semantic analysis: {e}") from e

    for mod in py_modules:
        mod.ast = res.graph[mod.module].tree
        # Use statically inferred __all__ if there is no runtime one.
        if mod.runtime_all is None:
            mod.runtime_all = res.manager.semantic_analyzer.export_map[mod.module]


def generate_stub_from_ast(
    mod: StubSource,
    target: str,
    parse_only: bool = False,
    include_private: bool = False,
    export_less: bool = False,
) -> None:
    """Use analysed (or just parsed) AST to generate type stub for single file.

    If directory for target doesn't exist it will created. Existing stub
    will be overwritten.
    """
    gen = StubGenerator(
        mod.runtime_all,
        include_private=include_private,
        analyzed=not parse_only,
        export_less=export_less,
    )
    assert mod.ast is not None, "This function must be used only with analyzed modules"
    mod.ast.accept(gen)

    # Write output to file.
    subdir = os.path.dirname(target)
    if subdir and not os.path.isdir(subdir):
        os.makedirs(subdir)
    with open(target, "w") as file:
        file.write("".join(gen.output()))


def collect_docs_signatures(doc_dir: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Gather all function and class signatures in the docs.

    Return a tuple (function signatures, class signatures).
    Currently only used for C modules.
    """
    all_sigs: List[Sig] = []
    all_class_sigs: List[Sig] = []
    for path in glob.glob(f"{doc_dir}/*.rst"):
        with open(path) as f:
            loc_sigs, loc_class_sigs = parse_all_signatures(f.readlines())
        all_sigs += loc_sigs
        all_class_sigs += loc_class_sigs
    sigs = dict(find_unique_signatures(all_sigs))
    class_sigs = dict(find_unique_signatures(all_class_sigs))
    return sigs, class_sigs


def generate_stubs(options: Options) -> None:
    """Main entry point for the program."""
    mypy_opts = mypy_options(options)
    py_modules, c_modules = collect_build_targets(options, mypy_opts)

    # Collect info from docs (if given):
    sigs = class_sigs = None  # type: Optional[Dict[str, str]]
    if options.doc_dir:
        sigs, class_sigs = collect_docs_signatures(options.doc_dir)

    # Use parsed sources to generate stubs for Python modules.
    generate_asts_for_modules(py_modules, options.parse_only, mypy_opts, options.verbose)
    files = []
    for mod in py_modules:
        assert mod.path is not None, "Not found module was not skipped"
        target = mod.module.replace(".", "/")
        if os.path.basename(mod.path) == "__init__.py":
            target += "/__init__.pyi"
        else:
            target += ".pyi"
        target = os.path.join(options.output_dir, target)
        files.append(target)
        with generate_guarded(mod.module, target, options.ignore_errors, options.verbose):
            generate_stub_from_ast(
                mod, target, options.parse_only, options.include_private, options.export_less
            )

    # Separately analyse C modules using different logic.
    for mod in c_modules:
        if any(py_mod.module.startswith(mod.module + ".") for py_mod in py_modules + c_modules):
            target = mod.module.replace(".", "/") + "/__init__.pyi"
        else:
            target = mod.module.replace(".", "/") + ".pyi"
        target = os.path.join(options.output_dir, target)
        files.append(target)
        with generate_guarded(mod.module, target, options.ignore_errors, options.verbose):
            generate_stub_for_c_module(mod.module, target, sigs=sigs, class_sigs=class_sigs)
    num_modules = len(py_modules) + len(c_modules)
    if not options.quiet and num_modules > 0:
        print("Processed %d modules" % num_modules)
        if len(files) == 1:
            print(f"Generated {files[0]}")
        else:
            print(f"Generated files under {common_dir_prefix(files)}" + os.sep)


HEADER = """%(prog)s [-h] [more options, see -h]
                     [-m MODULE] [-p PACKAGE] [files ...]"""

DESCRIPTION = """
Generate draft stubs for modules.

Stubs are generated in directory ./out, to avoid overriding files with
manual changes.  This directory is assumed to exist.
"""


def parse_options(args: List[str]) -> Options:
    parser = argparse.ArgumentParser(prog="stubgen", usage=HEADER, description=DESCRIPTION)

    parser.add_argument(
        "--ignore-errors",
        action="store_true",
        help="ignore errors when trying to generate stubs for modules",
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help="don't import the modules, just parse and analyze them "
        "(doesn't work with C extension modules and might not "
        "respect __all__)",
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="don't perform semantic analysis of sources, just parse them "
        "(only applies to Python modules, might affect quality of stubs)",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="generate stubs for objects and members considered private "
        "(single leading underscore and no trailing underscores)",
    )
    parser.add_argument(
        "--export-less",
        action="store_true",
        help=(
            "don't implicitly export all names imported from other modules " "in the same package"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show more verbose messages")
    parser.add_argument("-q", "--quiet", action="store_true", help="show fewer messages")
    parser.add_argument(
        "--doc-dir",
        metavar="PATH",
        default="",
        help="use .rst documentation in PATH (this may result in "
        "better stubs in some cases; consider setting this to "
        "DIR/Python-X.Y.Z/Doc/library)",
    )
    parser.add_argument(
        "--search-path",
        metavar="PATH",
        default="",
        help="specify module search directories, separated by ':' "
        "(currently only used if --no-import is given)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        dest="output_dir",
        default="out",
        help="change the output directory [default: %(default)s]",
    )
    parser.add_argument(
        "-m",
        "--module",
        action="append",
        metavar="MODULE",
        dest="modules",
        default=[],
        help="generate stub for module; can repeat for more modules",
    )
    parser.add_argument(
        "-p",
        "--package",
        action="append",
        metavar="PACKAGE",
        dest="packages",
        default=[],
        help="generate stubs for package recursively; can be repeated",
    )
    parser.add_argument(
        metavar="files",
        nargs="*",
        dest="files",
        help="generate stubs for given files or directories",
    )

    ns = parser.parse_args(args)

    pyversion = sys.version_info[:2]
    ns.interpreter = sys.executable

    if ns.modules + ns.packages and ns.files:
        parser.error("May only specify one of: modules/packages or files.")
    if ns.quiet and ns.verbose:
        parser.error("Cannot specify both quiet and verbose messages")

    # Create the output folder if it doesn't already exist.
    if not os.path.exists(ns.output_dir):
        os.makedirs(ns.output_dir)

    return Options(
        pyversion=pyversion,
        no_import=ns.no_import,
        doc_dir=ns.doc_dir,
        search_path=ns.search_path.split(":"),
        interpreter=ns.interpreter,
        ignore_errors=ns.ignore_errors,
        parse_only=ns.parse_only,
        include_private=ns.include_private,
        output_dir=ns.output_dir,
        modules=ns.modules,
        packages=ns.packages,
        files=ns.files,
        verbose=ns.verbose,
        quiet=ns.quiet,
        export_less=ns.export_less,
    )


def main(args: Optional[List[str]] = None) -> None:
    mypy.util.check_python_version("stubgen")
    # Make sure that the current directory is in sys.path so that
    # stubgen can be run on packages in the current directory.
    if not ("" in sys.path or "." in sys.path):
        sys.path.insert(0, "")

    options = parse_options(sys.argv[1:] if args is None else args)
    generate_stubs(options)


if __name__ == "__main__":
    main()
