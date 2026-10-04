"""录样例的记录器（tests/preview_recorder.py）：存成什么样，编号怎么变成固定的。"""

from __future__ import annotations

import json

import httpx
from preview_recorder import Recorder

FIRST = "3f9ea254-e52a-4cf8-aed3-979e52c9e3f3"
SECOND = "9c0de7a1-0c1e-4a57-b7a3-5d1f0c2e8b11"


def backend(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/things" and request.method == "GET":
        shown = [FIRST] if request.url.params.get("mine") else [FIRST, SECOND]
        return httpx.Response(200, json={"things": [{"id": i} for i in shown]})
    if path == f"/api/things/{SECOND}":
        return httpx.Response(
            200, json={"id": SECOND, "next_to": FIRST, "token": "t" * 43, "n": 1}
        )
    if path == "/api/things" and request.method == "POST":
        return httpx.Response(201, json={"id": SECOND})
    if path == "/api/export":
        return httpx.Response(
            200, text=f"# 导出\n\n{FIRST}\n", headers={"content-type": "text/markdown"}
        )
    return httpx.Response(404, json={"code": "not_found"})


async def recorded(tmp_path, scenario="full"):
    recorder = Recorder(scenario, title="什么都有", description="样例")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend"
    ) as http:
        listed = await recorder.get(http, "/api/things")
        assert [t["id"] for t in listed["things"]] == [FIRST, SECOND]
        await recorder.get(http, "/api/things", mine="1")
        await recorder.get(http, f"/api/things/{SECOND}")
        await recorder.call(http, "POST", "/api/things", expect=201, json_body={})
        await recorder.get(http, "/api/export")
        await recorder.get(http, "/api/nothing", expect=404)
    recorder.stream(f"/api/things/{SECOND}/stream", [{"cursor": 1, "about": FIRST}])
    recorder.page("一样东西", f"/zh-CN/things/{SECOND}")
    directory = recorder.write(tmp_path)
    return directory, json.loads((directory / "manifest.json").read_text())


async def test_what_is_written(tmp_path):
    directory, manifest = await recorded(tmp_path)
    assert directory == tmp_path / "full"
    assert manifest["id"] == "full" and manifest["signed_in"] is True
    assert [
        (r["method"], r["path"].rsplit("/", 1)[0], r["query"], r["status"])
        for r in manifest["responses"]
    ] == [
        ("GET", "/api", "", 200),
        ("GET", "/api", "mine=1", 200),
        ("GET", "/api/things", "", 200),
        ("POST", "/api", "", 201),
        ("GET", "/api", "", 200),
        ("GET", "/api", "", 404),
    ]
    export = next(r for r in manifest["responses"] if r["path"] == "/api/export")
    assert export["file"].endswith(".md")
    assert (directory / export["file"]).read_text().startswith("# 导出")
    assert len(manifest["streams"]) == 1 and len(manifest["pages"]) == 1


async def test_ids_become_fixed_and_stay_consistent(tmp_path):
    directory, manifest = await recorded(tmp_path)
    everything = "".join(p.read_text() for p in directory.iterdir())
    assert FIRST not in everything and SECOND not in everything
    listed = json.loads((directory / manifest["responses"][0]["file"]).read_text())
    first, second = (t["id"] for t in listed["things"])
    one = next(r for r in manifest["responses"] if r["path"] == f"/api/things/{second}")
    body = json.loads((directory / one["file"]).read_text())
    # 地址里的编号、内容里的编号、事件流与页面里的编号，是同一个
    assert body["id"] == second and body["next_to"] == first
    assert manifest["streams"][0]["path"] == f"/api/things/{second}/stream"
    assert manifest["pages"][0]["path"] == f"/zh-CN/things/{second}"
    stream = json.loads((directory / manifest["streams"][0]["file"]).read_text())
    assert stream == [{"cursor": 1, "about": first}]
    # 令牌也换成固定的
    assert body["token"].startswith("preview-token-0000") and len(body["token"]) == 43


async def test_recording_again_gives_the_same_addresses(tmp_path):
    _, first = await recorded(tmp_path / "a")
    _, again = await recorded(tmp_path / "b")
    assert first == again
    _, other = await recorded(tmp_path / "c", scenario="other")
    assert other["pages"] != first["pages"]  # 情景不同，编号不同


async def test_the_last_answer_for_an_address_wins(tmp_path):
    recorder = Recorder("full", title="", description="")
    seen = iter([{"state": "RUNNING"}, {"state": "SUCCEEDED"}])

    def changing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(seen))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(changing), base_url="http://backend"
    ) as http:
        await recorder.get(http, "/api/task")
        await recorder.get(http, "/api/task")
    directory = recorder.write(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    (only,) = manifest["responses"]
    assert json.loads((directory / only["file"]).read_text()) == {"state": "SUCCEEDED"}
