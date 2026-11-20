import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.builder_app import app as ba
t = TestClient(ba)

# empty body
r = t.post("/build", content=b"", headers={"content-type":"application/json"})
print("empty body:", r.status_code, r.json())

# only whitespace
r = t.post("/build", content=b"   ", headers={"content-type":"application/json"})
print("whitespace:", r.status_code, r.json())

# valid but all districts have empty block lists
r = t.post("/build", json={
  "old_districts":[{"code":"O1","blocks":[]}],
  "new_districts":[{"code":"N1","blocks":[]}]})
print("all-empty blocks:", r.status_code, r.json() if r.status_code!=200 else r.json()["new_district_relations"])

# not an object (array)
r = t.post("/build", content=b"[]", headers={"content-type":"application/json"})
print("array root:", r.status_code, r.json())

# null
r = t.post("/build", content=b"null", headers={"content-type":"application/json"})
print("null root:", r.status_code, r.json())

# missing one required top field
r = t.post("/build", content=b'{"old_districts":[{"code":"O1","blocks":[]}]}',
           headers={"content-type":"application/json"})
print("missing new:", r.status_code, r.json())
