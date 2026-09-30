"""Live check of the upload lifecycle against a RUNNING API and the REAL S3 bucket. Plays the browser's role.

    python scripts/upload_flow_check.py http://127.0.0.1:8000 path/to/audio.wav

Happy path: initiate -> PUT the bytes to the signed URL -> complete (twice) -> read back.
Abuse cases S3 itself must refuse: wrong Content-Type, wrong size. Plus: completing before uploading.
Leaves the test objects and job rows behind (they show up in the history list).

/complete hands the job to the worker. If a worker is running, the uploaded file is REALLY transcribed and
summarised (it uses Gnani and Groq quota) and the job moves on while this script runs, so the checks accept the
job being at any status after the upload. Stop the worker for a storage-only check: the job then stays QUEUED.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

failures: list[str] = []
ACCEPTED = {"UPLOADED", "QUEUED", "TRANSCRIBING", "SUMMARIZING", "COMPLETED"}  # anything after a verified upload


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        failures.append(name)


def initiate(api: httpx.Client, filename: str, size: int) -> dict:  # type: ignore[type-arg]
    res = api.post("/api/uploads/initiate", json={"filename": filename, "size_bytes": size})
    assert res.status_code == 201, res.text
    return res.json()  # type: ignore[no-any-return]


def main(base_url: str, audio: Path) -> int:
    data = audio.read_bytes()
    api = httpx.Client(base_url=base_url, timeout=60)

    print("1. happy path")
    job = initiate(api, audio.name, len(data))
    target = job["upload"]
    check("initiate returns UPLOADING + a PUT target", job["status"] == "UPLOADING" and target["method"] == "PUT")
    early = api.post(f"/api/uploads/{job['id']}/complete")
    check(
        "complete before uploading -> 409 UPLOAD_NOT_FOUND",
        early.status_code == 409 and early.json()["error"]["code"] == "UPLOAD_NOT_FOUND",
        early.text[:80],
    )
    put = httpx.put(target["url"], content=data, headers=target["headers"], timeout=120)
    check("browser-style PUT straight to S3 -> 200", put.status_code == 200, f"HTTP {put.status_code}")
    first = api.post(f"/api/uploads/{job['id']}/complete")
    check(
        "complete -> accepted (QUEUED, or later)",
        first.status_code == 200 and first.json()["status"] in ACCEPTED,
        first.text[:80],
    )
    second = api.post(f"/api/uploads/{job['id']}/complete")
    check(
        "second complete is a harmless repeat",
        second.status_code == 200 and second.json()["id"] == job["id"] and second.json()["status"] in ACCEPTED,
    )
    detail = api.get(f"/api/uploads/{job['id']}").json()
    check("detail shows the stored state", detail["status"] in ACCEPTED and detail["size_bytes"] == len(data))
    listed = api.get("/api/uploads").json()
    check("history lists it first", bool(listed) and listed[0]["id"] == job["id"])

    print("2. abuse cases S3 must refuse (signature covers Content-Type and Content-Length)")
    wrong_type = initiate(api, audio.name, len(data))
    res = httpx.put(wrong_type["upload"]["url"], content=data, headers={"Content-Type": "text/plain"}, timeout=120)
    check("wrong Content-Type -> 403", res.status_code == 403, f"HTTP {res.status_code}")
    wrong_size = initiate(api, audio.name, len(data))
    res = httpx.put(
        wrong_size["upload"]["url"], content=data[:-1], headers=wrong_size["upload"]["headers"], timeout=120
    )
    check("wrong size -> 403", res.status_code == 403, f"HTTP {res.status_code}")
    still = api.post(f"/api/uploads/{wrong_size['id']}/complete")
    check("after the refused upload, complete still says not uploaded", still.status_code == 409, still.text[:80])

    print("3. input validation")
    res = api.post("/api/uploads/initiate", json={"filename": "notes.txt", "size_bytes": 10})
    check("unsupported type -> 422 with a readable message", res.status_code == 422 and "not supported" in res.text)

    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1].rstrip("/"), Path(sys.argv[2])))
