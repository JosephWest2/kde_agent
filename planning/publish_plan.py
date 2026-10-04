"""Publish reviewed tracking issues and native subissues using authenticated gh.

Writes a local journal after every successful mutation. On an uncertain API
failure, stop; a subsequent invocation reconciles existing titles/relationships
before writing again. No shell interpolation or automatic mutation retries.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import time

from render_plan import ROOT, main as render_all, read, render

ENDPOINT = "repos/JosephWest2/kde_agent"
JOURNAL = ROOT / "published.json"


def api(path, method="GET", payload=None):
    command = ["gh", "api", path, "--method", method]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True)
    if method != "GET":
        time.sleep(1.1)  # Space content mutations to respect secondary limits.
    return json.loads(result.stdout) if result.stdout.strip() else None


def save(mapping):
    temporary = JOURNAL.with_suffix(".tmp")
    temporary.write_text(json.dumps(mapping, indent=2) + "\n")
    temporary.replace(JOURNAL)


def title(item):
    return item["title"] if "parent" not in item else f"[{item['id']}] {item['title']}"


def snapshot(issue):
    return {"number": issue["number"], "id": issue["id"], "url": issue["html_url"]}


def reviews_approved():
    for filename in ("milestones-review.md", "subissues-review.md"):
        report = (ROOT / "reviews" / filename).read_text()
        assert "**Final decision: APPROVED**" in report, f"Review not approved: {filename}"


def all_existing():
    results = []
    page = 1
    while True:
        batch = api(f"{ENDPOINT}/issues?state=all&per_page=100&page={page}")
        results.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            return results
        page += 1


def publish(milestones, children, mapping):
    reviews_approved()
    existing = all_existing()
    for item in milestones + children:
        key = item["id"]
        matches = [i for i in existing if i["title"] == title(item)]
        assert len(matches) <= 1, f"Ambiguous existing issue title: {key}"
        if key in mapping:
            assert matches and matches[0]["number"] == mapping[key]["number"], f"Journal conflict: {key}"
        elif matches:
            mapping[key] = snapshot(matches[0])
            save(mapping)
        else:
            issue = api(f"{ENDPOINT}/issues", "POST", {
                "title": title(item),
                "body": render(item, mapping, [c for c in children if c["parent"] == key]),
            })
            mapping[key] = snapshot(issue)
            save(mapping)
        print(f"Published {key}: {mapping[key]['url']}", flush=True)

    # At this point all IDs resolve to real issue references. Update only bodies
    # that differ (normally just the eight parents with their child lists).
    for item in milestones + children:
        number = mapping[item["id"]]["number"]
        expected = render(item, mapping, [c for c in children if c["parent"] == item["id"]])
        current = api(f"{ENDPOINT}/issues/{number}")
        if current["body"] != expected:
            api(f"{ENDPOINT}/issues/{number}", "PATCH", {"body": expected})
            print(f"Resolved links in {item['id']}", flush=True)

    for parent in milestones:
        number = mapping[parent["id"]]["number"]
        linked = {i["id"] for i in api(f"{ENDPOINT}/issues/{number}/sub_issues?per_page=100")}
        for child in [c for c in children if c["parent"] == parent["id"]]:
            child_id = mapping[child["id"]]["id"]
            if child_id not in linked:
                api(f"{ENDPOINT}/issues/{number}/sub_issues", "POST", {"sub_issue_id": child_id})
            print(f"Linked {child['id']} under {parent['id']}", flush=True)


def verify(milestones, children, mapping):
    assert set(mapping) == {i["id"] for i in milestones + children}

    def check_item(item):
        actual = api(f"{ENDPOINT}/issues/{mapping[item['id']]['number']}")
        expected = render(item, mapping, [c for c in children if c["parent"] == item["id"]])
        assert actual["title"] == title(item), item["id"]
        assert actual["body"] == expected, f"Body mismatch: {item['id']}"
        assert actual["state"] == "open", item["id"]
        return item["id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        checked = list(executor.map(check_item, milestones + children))
    for parent in milestones:
        actual = api(f"{ENDPOINT}/issues/{mapping[parent['id']]['number']}/sub_issues?per_page=100")
        expected = {mapping[c["id"]]["id"] for c in children if c["parent"] == parent["id"]}
        assert {i["id"] for i in actual} == expected, f"Subissue relationship mismatch: {parent['id']}"
    evidence = {
        "repository": "JosephWest2/kde_agent", "issues_verified": len(checked),
        "milestones": len(milestones), "native_subissue_links": len(children),
        "spec_sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
                        for f in ("milestones.json", "subissues.json")},
    }
    (ROOT / "publication-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Verified {len(checked)} open issues, exact reviewed bodies and {len(children)} native subissue relationships.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    render_all()
    milestones, children = read("milestones.json"), read("subissues.json")
    mapping = read("published.json") if JOURNAL.exists() else {}
    if args.publish:
        publish(milestones, children, mapping)
        render_all()
    if args.verify:
        verify(milestones, children, mapping)
