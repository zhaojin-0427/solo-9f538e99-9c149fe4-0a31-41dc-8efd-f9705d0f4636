"""Schema-driven incremental JSON parser for the large /build request.

The request shape is fixed::

    {
      "old_districts": [{"code": str, "blocks": [str, ...]}, ...],
      "new_districts": [{"code": str, "blocks": [str, ...]}, ...]
    }

Instead of loading the whole document (up to hundreds of MiB of text) into a
Python object tree, the parser walks the JSON grammar one character at a
time and pushes every block id straight into the
:class:`~app.builder_core.CoverageAccumulator`.  Strings are scanned by hand
(quote plus JSON escape rules), so every production is naturally
incremental: a token split across network chunks simply resumes when the
next chunk arrives.  Each grammar frame on the stack remembers the exact
production step it is on, which makes a chunk boundary at any byte harmless.

The buffer always starts at the next unprocessed character, so peak parser
memory is a few KiB plus the accumulator's compact per-block table.

The whole request is always consumed: validation and JSON errors are recorded
and raised only once the end of the stream is reached, so a request whose
tail is truncated or corrupted still fails atomically with a reason.
"""
import codecs
from typing import List, Optional

from .builder_core import BuilderConstraintError, CoverageAccumulator


class StreamParseError(ValueError):
    """The request body is not syntactically valid JSON (mapped to HTTP 400)."""


_WS = frozenset(" \t\r\n")
_DECODER_FACTORY = codecs.getincrementaldecoder("utf-8")
_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class _Frame:
    """One grammar production and the step currently in progress."""

    __slots__ = (
        "kind",
        "step",
        "block_ids",
        "seen_keys",
        "side",
        "index",
        "pending_key",
        "pending_phase",
        "comma_done",
    )

    def __init__(self, kind: str, side: Optional[str] = None, index: int = -1):
        self.kind = kind  # "top" | "district_list" | "district_obj" | "block_list"
        # Step within the production; marks how many elements/fields done.
        self.step = 0
        self.block_ids: set = set()
        self.seen_keys: set = set()
        self.side = side
        self.index = index
        # A key/string already read whose ':' and value span a chunk boundary.
        self.pending_key: Optional[str] = None
        # Punctuation already consumed after pending_key: "", ":" or ":["
        # (the latter only transiently, since "[" completes the production).
        self.pending_phase = ""
        # A separating comma already consumed before the next element.
        self.comma_done = False


class StreamingBuildParser:
    def __init__(self, n_old_limit: int, n_new_limit: int):
        self._text_decoder = _DECODER_FACTORY()
        # Always starts at the next character the state machine must inspect.
        self._buffer = ""
        self._finished = False

        self.old_codes: List[str] = []
        self.new_codes: List[str] = []
        # Created up front: old-side bits use the district index directly,
        # so they can be recorded before n_old is finalised.
        self.accumulator: CoverageAccumulator = CoverageAccumulator()

        self._stack: List[_Frame] = []
        self._top_seen: set = set()
        # Set once the top-level '}' has been read; only trailing whitespace
        # is legal from here until the stream actually ends.
        self._closed = False
        self._pending_error: Optional[Exception] = None
        self._n_old_limit = n_old_limit
        self._n_new_limit = n_new_limit

    # ------------------------------------------------------------------
    # Public driver
    # ------------------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        if self._finished:
            return
        try:
            text = self._text_decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            self._fail(StreamParseError(f"request body is not valid UTF-8: {exc}"))
            return
        if text:
            self._buffer += text
            if self._closed:
                self._check_trailing()
            else:
                self._pump()

    def finish(self) -> CoverageAccumulator:
        if self._finished:
            return self._raise_pending()
        try:
            tail = self._text_decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            self._fail(StreamParseError(f"request body is not valid UTF-8: {exc}"))
            tail = ""
        if tail:
            self._buffer += tail
        if self._closed:
            self._check_trailing()
        else:
            self._pump()
        if self._pending_error:
            return self._raise_pending()
        if not self._closed:
            self._fail(
                StreamParseError(
                    "request body is truncated: JSON document is incomplete"
                )
            )
        return self._raise_pending()

    def _check_trailing(self) -> None:
        """After the top-level '}', only whitespace is allowed until EOF."""
        for ch in self._buffer:
            if ch not in _WS:
                self._fail(
                    StreamParseError(
                        "malformed JSON: trailing data after the request object"
                    )
                )
                return
        self._buffer = ""

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------
    def _fail(self, exc: Exception) -> None:
        if self._pending_error is None:
            self._pending_error = exc
        self._finished = True

    def _raise_pending(self) -> CoverageAccumulator:
        if self._pending_error is not None:
            raise self._pending_error
        if not self.accumulator._membership and not self.old_codes:
            raise StreamParseError("empty request body")
        return self.accumulator

    # ------------------------------------------------------------------
    # Buffer primitives
    # ------------------------------------------------------------------
    def _skip_ws(self) -> None:
        buf = self._buffer
        i = 0
        n = len(buf)
        while i < n and buf[i] in _WS:
            i += 1
        if i:
            self._buffer = buf[i:]

    def _peek(self) -> Optional[str]:
        self._skip_ws()
        return self._buffer[0] if self._buffer else None

    def _consume(self, ch: str) -> Optional[bool]:
        """True if consumed, False on mismatch (error recorded), None if
        the buffer ran dry."""
        seen = self._peek()
        if seen is None:
            return None
        if seen != ch:
            self._fail(
                StreamParseError(f"malformed JSON: expected {ch!r}, found {seen!r}")
            )
            return False
        self._buffer = self._buffer[1:]
        return True

    def _read_string(self) -> Optional[str]:
        """Scan one JSON string at buffer start.

        Returns the decoded string, ``None`` when more bytes are needed
        (partial text stays buffered), or records an error and returns None.
        """
        self._skip_ws()
        if not self._buffer:
            return None
        if self._buffer[0] != '"':
            self._fail(
                StreamParseError(
                    f"malformed JSON: expected a string, found {self._buffer[0]!r}"
                )
            )
            return None
        out: List[str] = []
        i = 1
        n = len(self._buffer)
        while True:
            if i >= n:
                return None  # split across chunks; retry on next feed
            ch = self._buffer[i]
            if ch == '"':
                i += 1
                break
            if ch == "\\":
                if i + 1 >= n:
                    return None
                esc = self._buffer[i + 1]
                if esc == "u":
                    if i + 6 > n:
                        return None
                    hexd = self._buffer[i + 2 : i + 6]
                    try:
                        out.append(chr(int(hexd, 16)))
                    except ValueError:
                        self._fail(
                            StreamParseError(
                                f"malformed JSON: bad \\u escape {hexd!r}"
                            )
                        )
                        return None
                    i += 6
                    continue
                if esc not in _ESCAPES:
                    self._fail(
                        StreamParseError(
                            f"malformed JSON: bad escape character {esc!r}"
                        )
                    )
                    return None
                out.append(_ESCAPES[esc])
                i += 2
                continue
            if ord(ch) < 0x20:
                self._fail(
                    StreamParseError(
                        "malformed JSON: unescaped control character in string"
                    )
                )
                return None
            out.append(ch)
            i += 1
        value = "".join(out)
        self._buffer = self._buffer[i:]
        return value

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------
    def _pump(self) -> None:
        while not self._finished and self._buffer:
            frame = self._stack[-1] if self._stack else None
            if frame is None:
                if self._enter_top() is None:
                    return
            elif frame.kind == "top":
                if self._parse_top(frame) is None:
                    return
            elif frame.kind == "district_list":
                if self._parse_district_list(frame) is None:
                    return
            elif frame.kind == "district_obj":
                if self._parse_district_object(frame) is None:
                    return
            elif frame.kind == "block_list":
                if self._parse_block_list(frame) is None:
                    return
            else:  # pragma: no cover - defensive
                self._fail(StreamParseError(f"internal frame {frame.kind!r}"))
                return

    def _enter_top(self):
        if self._consume("{") is not True:
            return None
        self._stack.append(_Frame("top"))
        return True

    # -- top-level object ------------------------------------------------
    def _parse_top(self, frame: _Frame):
        ch = self._peek()
        if ch is None:
            return None
        if ch == "}":
            self._buffer = self._buffer[1:]
            self._stack.pop()
            missing = {"old_districts", "new_districts"} - self._top_seen
            if missing:
                self._fail(
                    BuilderConstraintError(
                        f"missing required field(s): {sorted(missing)}"
                    )
                )
                return None
            self._materialize_accumulator()
            # Enter the trailing-whitespace phase; the document is complete
            # but any further non-whitespace byte must reject the request.
            self._closed = True
            self._check_trailing()
            return True

        if frame.step > 0 and not frame.comma_done:
            if self._consume(",") is not True:
                return None
            frame.comma_done = True
        # Resume a key whose ':' / opening '[' landed in a later chunk.
        if frame.pending_key is not None:
            key = frame.pending_key
        else:
            key = self._read_string()
            if key is None and not self._pending_error:
                return None
            if self._pending_error:
                return None
            if key in self._top_seen:
                self._fail(
                    BuilderConstraintError(f"duplicate top-level field {key!r}")
                )
                return None
            if key not in ("old_districts", "new_districts"):
                self._fail(
                    BuilderConstraintError(f"unexpected top-level field {key!r}")
                )
                return None
            frame.pending_key = key
        # From here the key is accepted; the punctuation may still be split.
        # pending_phase records punctuation already consumed so a chunk
        # boundary never re-consumes a ':' or '['.
        if frame.pending_phase == "":
            if self._consume(":") is not True:
                return None
            frame.pending_phase = ":"
        if self._consume("[") is not True:
            return None
        self._top_seen.add(key)
        frame.step += 1
        frame.comma_done = False
        side = "old" if key == "old_districts" else "new"
        frame.pending_key = None
        frame.pending_phase = ""
        self._stack.append(_Frame("district_list", side=side))
        return True

    # -- district array --------------------------------------------------
    def _parse_district_list(self, frame: _Frame):
        ch = self._peek()
        if ch is None:
            return None
        if ch == "]":
            self._buffer = self._buffer[1:]
            if frame.step == 0:
                self._fail(
                    BuilderConstraintError(
                        f"{frame.side}_districts must not be empty"
                    )
                )
                return None
            self._stack.pop()
            if frame.side == "old":
                self.accumulator.n_old = len(self.old_codes)
            else:
                self.accumulator.set_dimensions(
                    len(self.old_codes), len(self.new_codes)
                )
            return True
        if frame.step > 0 and not frame.comma_done:
            if self._consume(",") is not True:
                return None
            frame.comma_done = True
        limit = (
            self._n_old_limit if frame.side == "old" else self._n_new_limit
        )
        if frame.step >= limit:
            self._fail(
                BuilderConstraintError(
                    f"more than {limit} {frame.side} districts are not supported"
                )
            )
            return None
        if self._consume("{") is not True:
            return None
        frame.step += 1
        frame.comma_done = False
        self._stack.append(
            _Frame("district_obj", side=frame.side, index=frame.step - 1)
        )
        return True

    # -- district object -------------------------------------------------
    def _parse_district_object(self, frame: _Frame):
        ch = self._peek()
        if ch is None:
            return None
        if ch == "}":
            self._buffer = self._buffer[1:]
            missing = {"code", "blocks"} - frame.seen_keys
            if missing:
                self._fail(
                    BuilderConstraintError(
                        f"district object missing field(s): {sorted(missing)}"
                    )
                )
                return None
            self._stack.pop()
            return True
        if frame.seen_keys and not frame.comma_done:
            if self._consume(",") is not True:
                return None
            frame.comma_done = True
        if frame.pending_key is not None:
            field = frame.pending_key
        else:
            field = self._read_string()
            if field is None and not self._pending_error:
                return None
            if self._pending_error:
                return None
            if field in frame.seen_keys:
                self._fail(
                    BuilderConstraintError(
                        f"duplicate field {field!r} in district"
                    )
                )
                return None
            if field not in ("code", "blocks"):
                self._fail(
                    BuilderConstraintError(
                        f"unexpected district field {field!r}; "
                        "only code/blocks allowed"
                    )
                )
                return None
            frame.pending_key = field
            frame.pending_phase = ""
        if frame.pending_phase == "":
            if self._consume(":") is not True:
                return None
            frame.pending_phase = ":"
        if field == "code":
            code = self._read_string()
            if code is None and not self._pending_error:
                return None
            if self._pending_error:
                return None
            if not code:
                self._fail(
                    BuilderConstraintError("district code must be a non-empty string")
                )
                return None
            self._register_code(code, frame.side)
            if self._pending_error:
                return None
            frame.pending_key = None
            frame.pending_phase = ""
            frame.comma_done = False
            frame.seen_keys.add("code")
            return True
        # blocks
        if self._consume("[") is not True:
            return None
        frame.pending_key = None
        frame.pending_phase = ""
        frame.comma_done = False
        frame.seen_keys.add("blocks")
        self._stack.append(
            _Frame("block_list", side=frame.side, index=frame.index)
        )
        return True

    def _register_code(self, code: str, side: str) -> None:
        if side == "old":
            if code in self.old_codes:
                self._fail(
                    BuilderConstraintError(
                        f"duplicate district code in old_districts: {code!r}"
                    )
                )
                return
            self.old_codes.append(code)
        else:
            if code in self.new_codes:
                self._fail(
                    BuilderConstraintError(
                        f"duplicate district code in new_districts: {code!r}"
                    )
                )
                return
            self.new_codes.append(code)

    # -- block id array --------------------------------------------------
    def _parse_block_list(self, frame: _Frame):
        ch = self._peek()
        if ch is None:
            return None
        if ch == "]":
            self._buffer = self._buffer[1:]
            self._stack.pop()
            return True
        if frame.block_ids and not frame.comma_done:
            if self._consume(",") is not True:
                return None
            frame.comma_done = True
        block_id = self._read_string()
        if block_id is None and not self._pending_error:
            return None
        if self._pending_error:
            return None
        if block_id in frame.block_ids:
            codes = self.old_codes if frame.side == "old" else self.new_codes
            district = codes[frame.index]
            self._fail(
                BuilderConstraintError(
                    f"duplicate block id {block_id!r} in district {district!r}"
                )
            )
            return None
        frame.block_ids.add(block_id)
        frame.comma_done = False
        self.accumulator.add(block_id, frame.side, frame.index)
        return True

    def _materialize_accumulator(self) -> None:
        # Kept for compatibility; the accumulator now exists from the start.
        if self.accumulator.n_old == 0:
            self.accumulator.set_dimensions(
                len(self.old_codes), len(self.new_codes)
            )
