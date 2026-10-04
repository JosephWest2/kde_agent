"""Render reviewable GitHub issue bodies and validate the implementation plan."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "https://github.com/JosephWest2/kde_agent"
BASE = "9e49b06e651764e33ee94aab67db304856401170"


def read(name):
    return json.loads((ROOT / name).read_text())


def ref(key, published):
    return f"#{published[key]['number']}" if key in published else f"`{key}`"


def bullets(items, checkbox=False):
    return "\n".join(("- [ ] " if checkbox else "- ") + x for x in items)


def render(item, published, children):
    parts = ["## Outcome", item["outcome"]]
    if "parent" in item:
        parts += ["## Milestone", ref(item["parent"], published)]
    parts += ["## Dependencies", ", ".join(ref(k, published) for k in item["depends_on"]) or "None."]
    parts += ["## Scope", bullets(item["scope"]), "## Acceptance criteria", bullets(item["acceptance"], True)]
    if children:
        parts += ["## Subissues", bullets([f"{ref(c['id'], published)} — {c['title']}" for c in children], True)]
    parts += ["## Requirement coverage", ", ".join(item["requirements"])]
    if item.get("exclusions"):
        parts += ["## Boundaries", item["exclusions"]]
    parts += ["## Source contract", f"[Requirements]({REPO}/blob/{BASE}/REQUIREMENTS.md) and [proposed architecture]({REPO}/blob/{BASE}/ARCHITECTURE.md), baseline `{BASE[:12]}`. Requirements are authoritative; record evidence for architecture changes. Scope is the first supported headless version."]
    return "\n\n".join(parts) + "\n"


def main():
    milestones = read("milestones.json")
    subissues = read("subissues.json") if (ROOT / "subissues.json").exists() else []
    published = read("published.json") if (ROOT / "published.json").exists() else {}
    items = milestones + subissues
    by_id = {i["id"]: i for i in items}
    assert len(by_id) == len(items), "Duplicate issue IDs"
    valid = {f"REQ-{i:03}" for i in range(1, 51)}
    for item in items:
        assert set(item["requirements"]) <= valid, item["id"]
        assert all(k in by_id for k in item["depends_on"]), item["id"]
        assert item["scope"] and item["acceptance"]
        if "parent" in item:
            assert item["parent"] in {m["id"] for m in milestones}
            assert item["parent"] not in item["depends_on"], "Child cannot depend on its parent's completion"
    # A milestone completes only after its children; include those edges to catch
    # cycles hidden by a child's dependency on another milestone.
    def visit(key, stack):
        assert key not in stack, f"Dependency cycle: {stack + [key]}"
        for dep in by_id[key]["depends_on"] + [c["id"] for c in subissues if c["parent"] == key]:
            visit(dep, stack + [key])
    for key in by_id:
        visit(key, [])
    covered = {r for i in milestones for r in i["requirements"]}
    assert {f"REQ-{i:03}" for i in range(1, 47)} <= covered, "Missing MUST/SHOULD milestone coverage"
    if subissues:
        covered = {r for i in subissues for r in i["requirements"]}
        assert {f"REQ-{i:03}" for i in range(1, 47)} <= covered, "Missing MUST/SHOULD subissue coverage"
        for m in milestones:
            assert any(c["parent"] == m["id"] for c in subissues)
    output = ROOT / "drafts"
    output.mkdir(exist_ok=True)
    for item in items:
        (output / f"{item['id']}.md").write_text(render(item, published, [c for c in subissues if c["parent"] == item["id"]]))
    matrix = ["# Requirement coverage", "", "MUST: REQ-001–040. SHOULD: REQ-041–046. MAY: REQ-047–050 deferred.", "", "| Requirement | Milestones | Subissues |", "| --- | --- | --- |"]
    for n in range(1, 47):
        req = f"REQ-{n:03}"
        matrix.append(f"| {req} | {', '.join(ref(i['id'], published) for i in milestones if req in i['requirements'])} | {', '.join(ref(i['id'], published) for i in subissues if req in i['requirements'])} |")
    (ROOT / "COVERAGE.md").write_text("\n".join(matrix) + "\n")
    index = ["# Implementation issues", "", "See [README.md](README.md) for sequencing and scope and [COVERAGE.md](COVERAGE.md) for requirement mapping.", ""]
    for milestone in milestones:
        key = milestone["id"]
        link = published[key]["url"] if key in published else f"drafts/{key}.md"
        index += [f"## [{milestone['title']}]({link})", "", milestone["outcome"], ""]
        for child in [c for c in subissues if c["parent"] == key]:
            child_link = published[child["id"]]["url"] if child["id"] in published else f"drafts/{child['id']}.md"
            index.append(f"- [{child['id']}: {child['title']}]({child_link})")
        index.append("")
    (ROOT / "INDEX.md").write_text("\n".join(index))
    print(f"Validated and rendered {len(milestones)} milestones and {len(subissues)} subissues.")


if __name__ == "__main__":
    main()
