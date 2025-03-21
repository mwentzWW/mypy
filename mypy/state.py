from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from typing_extensions import Final

# These are global mutable state. Don't add anything here unless there's a very
# good reason.


class StrictOptionalState:
    # Wrap this in a class since it's faster that using a module-level attribute.

    def __init__(self, strict_optional: bool) -> None:
        # Value varies by file being processed
        self.strict_optional = strict_optional

    @contextmanager
    def strict_optional_set(self, value: bool) -> Iterator[None]:
        saved = self.strict_optional
        self.strict_optional = value
        try:
            yield
        finally:
            self.strict_optional = saved


state: Final = StrictOptionalState(strict_optional=False)
find_occurrences: Optional[Tuple[str, str]] = None
