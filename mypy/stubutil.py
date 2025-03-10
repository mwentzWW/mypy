"""Utilities for mypy.stubgen, mypy.stubgenc, and mypy.stubdoc modules."""

import os.path
import re
import sys
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple, Union

from typing_extensions import overload

from mypy.modulefinder import ModuleNotFoundReason
from mypy.moduleinspect import InspectError, ModuleInspect

# Modules that may fail when imported, or that may have side effects (fully qualified).
NOT_IMPORTABLE_MODULES = ()


class CantImport(Exception):
    def __init__(self, module: str, message: str):
        self.module = module
        self.message = message


def walk_packages(
    inspect: ModuleInspect, packages: List[str], verbose: bool = False
) -> Iterator[str]:
    """Iterates through all packages and sub-packages in the given list.

    This uses runtime imports (in another process) to find both Python and C modules.
    For Python packages we simply pass the __path__ attribute to pkgutil.walk_packages() to
    get the content of the package (all subpackages and modules).  However, packages in C
    extensions do not have this attribute, so we have to roll out our own logic: recursively
    find all modules imported in the package that have matching names.
    """
    for package_name in packages:
        if package_name in NOT_IMPORTABLE_MODULES:
            print(f"{package_name}: Skipped (blacklisted)")
            continue
        if verbose:
            print(f"Trying to import {package_name!r} for runtime introspection")
        try:
            prop = inspect.get_package_properties(package_name)
        except InspectError:
            report_missing(package_name)
            continue
        yield prop.name
        if prop.is_c_module:
            # Recursively iterate through the subpackages
            yield from walk_packages(inspect, prop.subpackages, verbose)
        else:
            yield from prop.subpackages


def find_module_path_using_sys_path(module: str, sys_path: List[str]) -> Optional[str]:
    relative_candidates = (
        module.replace(".", "/") + ".py",
        os.path.join(module.replace(".", "/"), "__init__.py"),
    )
    for base in sys_path:
        for relative_path in relative_candidates:
            path = os.path.join(base, relative_path)
            if os.path.isfile(path):
                return path
    return None


def find_module_path_and_all_py3(
    inspect: ModuleInspect, module: str, verbose: bool
) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
    """Find module and determine __all__ for a Python 3 module.

    Return None if the module is a C module. Return (module_path, __all__) if
    it is a Python module. Raise CantImport if import failed.
    """
    if module in NOT_IMPORTABLE_MODULES:
        raise CantImport(module, "")

    # TODO: Support custom interpreters.
    if verbose:
        print(f"Trying to import {module!r} for runtime introspection")
    try:
        mod = inspect.get_package_properties(module)
    except InspectError as e:
        # Fall back to finding the module using sys.path.
        path = find_module_path_using_sys_path(module, sys.path)
        if path is None:
            raise CantImport(module, str(e)) from e
        return path, None
    if mod.is_c_module:
        return None
    return mod.file, mod.all


@contextmanager
def generate_guarded(
    mod: str, target: str, ignore_errors: bool = True, verbose: bool = False
) -> Iterator[None]:
    """Ignore or report errors during stub generation.

    Optionally report success.
    """
    if verbose:
        print(f"Processing {mod}")
    try:
        yield
    except Exception as e:
        if not ignore_errors:
            raise e
        else:
            # --ignore-errors was passed
            print("Stub generation failed for", mod, file=sys.stderr)
    else:
        if verbose:
            print(f"Created {target}")


def report_missing(mod: str, message: Optional[str] = "", traceback: str = "") -> None:
    if message:
        message = " with error: " + message
    print(f"{mod}: Failed to import, skipping{message}")


def fail_missing(mod: str, reason: ModuleNotFoundReason) -> None:
    if reason is ModuleNotFoundReason.NOT_FOUND:
        clarification = "(consider using --search-path)"
    elif reason is ModuleNotFoundReason.FOUND_WITHOUT_TYPE_HINTS:
        clarification = "(module likely exists, but is not PEP 561 compatible)"
    else:
        clarification = f"(unknown reason '{reason}')"
    raise SystemExit(f"Can't find module '{mod}' {clarification}")


@overload
def remove_misplaced_type_comments(source: bytes) -> bytes:
    ...


@overload
def remove_misplaced_type_comments(source: str) -> str:
    ...


def remove_misplaced_type_comments(source: Union[str, bytes]) -> Union[str, bytes]:
    """Remove comments from source that could be understood as misplaced type comments.

    Normal comments may look like misplaced type comments, and since they cause blocking
    parse errors, we want to avoid them.
    """
    if isinstance(source, bytes):
        # This gives us a 1-1 character code mapping, so it's roundtrippable.
        text = source.decode("latin1")
    else:
        text = source

    # Remove something that looks like a variable type comment but that's by itself
    # on a line, as it will often generate a parse error (unless it's # type: ignore).
    text = re.sub(r'^[ \t]*# +type: +["\'a-zA-Z_].*$', "", text, flags=re.MULTILINE)

    # Remove something that looks like a function type comment after docstring,
    # which will result in a parse error.
    text = re.sub(r'""" *\n[ \t\n]*# +type: +\(.*$', '"""\n', text, flags=re.MULTILINE)
    text = re.sub(r"''' *\n[ \t\n]*# +type: +\(.*$", "'''\n", text, flags=re.MULTILINE)

    # Remove something that looks like a badly formed function type comment.
    text = re.sub(r"^[ \t]*# +type: +\([^()]+(\)[ \t]*)?$", "", text, flags=re.MULTILINE)

    if isinstance(source, bytes):
        return text.encode("latin1")
    else:
        return text


def common_dir_prefix(paths: List[str]) -> str:
    if not paths:
        return "."
    cur = os.path.dirname(os.path.normpath(paths[0]))
    for path in paths[1:]:
        while True:
            path = os.path.dirname(os.path.normpath(path))
            if (cur + os.sep).startswith(path + os.sep):
                cur = path
                break
    return cur or "."
