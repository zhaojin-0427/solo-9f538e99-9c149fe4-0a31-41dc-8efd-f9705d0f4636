import sys
sys.path.insert(0, "/workspace")
from app.streaming import StreamingBuildParser, StreamParseError
from app.builder_core import BuilderConstraintError

cases = {
    "trailing garbage": b'{"old_districts":[{"code":"O1","blocks":["a"]}],"new_districts":[{"code":"N1","blocks":["a"]}]} @garbage',
    "truncated mid-string": b'{"old_districts":[{"code":"O1","blocks":["abc',
    "missing brace": b'{"old_districts":[{"code":"O1","blocks":["a"]],"new_districts":[]}',
    "bad token": b'{"old_districts":[{"code":"O1","blocks":[tru]}],"new_districts":[]}',
}
for name, payload in cases.items():
    p = StreamingBuildParser(8, 8)
    # feed byte at a time to stress boundary handling
    for i in range(len(payload)):
        p.feed(payload[i:i+1])
    try:
        p.finish()
        print(f"{name}: NO ERROR (unexpected)")
    except (StreamParseError, BuilderConstraintError) as e:
        print(f"{name}: {type(e).__name__}: {e}")

# Duplicate block id appearing in the FINAL district (tail of request).
p = StreamingBuildParser(8, 8)
tail = b'{"old_districts":[{"code":"O1","blocks":["a"]}],"new_districts":[{"code":"N9","blocks":["z","z"]}]}'
p.feed(tail)
try:
    p.finish()
except BuilderConstraintError as e:
    print("tail duplicate:", e)
