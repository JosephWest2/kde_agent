"""One-way GitHub issue/PR metadata to project Status synchronization."""

import argparse
import json
import os
import re
import subprocess


STATUS_LABELS = (
    ('blocked', 'Blocked'),
    ('in-progress', 'In progress'),
    ('review', 'Review'),
    ('backlog', 'Backlog'),
)


def select_status(issue):
    if issue['state'] == 'closed':
        return 'Done'
    if issue['state'] != 'open':
        raise ValueError('unrecognized issue state')
    labels = {label['name'].casefold() for label in issue['labels']}
    for label, status in STATUS_LABELS:
        if label in labels:
            return status
    if 'pull_request' in issue:
        return 'In progress' if issue['draft'] else 'Review'
    return 'Todo'


PROJECT_INFO = '''query ProjectInfo($project: ID!) {
  node(id: $project) { ... on ProjectV2 {
    id repositories(first: 100) { pageInfo { hasNextPage } nodes { nameWithOwner } }
    fields(first: 100) { pageInfo { hasNextPage } nodes {
      ... on ProjectV2SingleSelectField { id name options { id name } }
    } }
  } }
}'''
PROJECT_ITEMS = '''query ProjectItems($project: ID!, $cursor: String) {
  node(id: $project) { ... on ProjectV2 {
    items(first: 100, after: $cursor, archivedStates: [ARCHIVED, NOT_ARCHIVED]) { pageInfo { hasNextPage endCursor } nodes {
      id isArchived
      content {
        ... on Issue { id number repository { nameWithOwner } }
        ... on PullRequest { id number repository { nameWithOwner } }
      }
      fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    } }
  } }
}'''
ADD_ITEM = '''mutation AddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: { projectId: $project, contentId: $content }) { item { id isArchived } }
}'''
SET_STATUS = '''mutation SetStatus($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}'''
VERIFY_ITEM = '''query VerifyItem($item: ID!) {
  node(id: $item) { ... on ProjectV2Item {
    project { id } content { ... on Issue { id } ... on PullRequest { id } }
    fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
  } }
}'''


class GitHub:
    def request(self, args, payload=None):
        command = ['gh', 'api', '--hostname', 'github.com', *args]
        if payload is not None:
            command.extend(['--input', '-'])
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=90, check=False)
        if result.returncode:
            raise RuntimeError('GitHub API request failed: ' + result.stderr.strip()[:400])
        return json.loads(result.stdout)

    def rest(self, path, paginate=False):
        args = ['--method', 'GET', path]
        if paginate:
            args.extend(['--paginate', '--slurp'])
        result = self.request(args)
        return [item for page in result for item in page] if paginate else result

    def graphql(self, query, **variables):
        result = self.request(['graphql'], {'query': query, 'variables': variables})
        if result.get('errors'):
            raise RuntimeError('GitHub GraphQL request failed: ' + json.dumps(result['errors'])[:400])
        return result['data']


class Syncer:
    def __init__(self, api, repo, project_id):
        self.api = api
        self.repo = repo
        self.project_id = project_id

    def load_items(self):
        items = {}
        cursor = None
        while True:
            page = self.api.graphql(PROJECT_ITEMS, project=self.project_id, cursor=cursor)['node']['items']
            for item in page['nodes']:
                content = item.get('content') or {}
                if (content.get('repository') or {}).get('nameWithOwner') == self.repo:
                    items[content['number']] = item
            if not page['pageInfo']['hasNextPage']:
                return items
            cursor = page['pageInfo']['endCursor']

    def run(self, numbers=None, dry_run=False):
        project = self.api.graphql(PROJECT_INFO, project=self.project_id)['node']
        if project is None or self.repo not in {r['nameWithOwner'] for r in project['repositories']['nodes']}:
            raise ValueError('project is inaccessible or not linked to this repository')
        if project['repositories']['pageInfo']['hasNextPage']:
            raise ValueError('linked repository list exceeds the supported bound')
        if project['fields']['pageInfo']['hasNextPage']:
            raise ValueError('project field list exceeds the supported bound')
        field = next((f for f in project['fields']['nodes'] if f.get('name') == 'Status'), None)
        if field is None:
            raise ValueError('project is missing the single-select Status field')
        options = {o['name']: o['id'] for o in field['options']}
        required = {status for _, status in STATUS_LABELS} | {'Todo', 'Done'}
        if required - options.keys():
            raise ValueError('project is missing required Status options')
        items = self.load_items()
        if numbers is None:
            opened = self.api.rest(f'repos/{self.repo}/issues?state=open&per_page=100', paginate=True)
            numbers = set(items) | {issue['number'] for issue in opened}
        result = {'processed': 0, 'added': 0, 'changed': 0, 'unchanged': 0,
                  'would_add': 0, 'would_change': 0, 'skipped': 0}
        for number in sorted(set(numbers)):
            if (items.get(number) or {}).get('isArchived'):
                result['skipped'] += 1
                continue
            issue = self.api.rest(f'repos/{self.repo}/issues/{number}')
            if 'pull_request' in issue and issue['state'] == 'open':
                issue['draft'] = self.api.rest(f'repos/{self.repo}/pulls/{number}')['draft']
            status = select_status(issue)
            item = items.get(number)
            if item is None:
                if dry_run:
                    result['would_add'] += 1
                    item = {}
                else:
                    try:
                        item = self.api.graphql(ADD_ITEM, project=self.project_id, content=issue['node_id'])['addProjectV2ItemById']['item']
                        if not item['isArchived']:
                            result['added'] += 1
                    except RuntimeError as error:
                        if 'Content already exists' not in str(error):
                            raise
                        item = self.load_items().get(number)
                        if item is None or item['content']['id'] != issue['node_id']:
                            raise RuntimeError('concurrent project addition could not be verified') from error
                    if item.get('isArchived'):
                        result['skipped'] += 1
                        continue
            result['processed'] += 1
            if (item.get('fieldValueByName') or {}).get('name') == status:
                result['unchanged'] += 1
                continue
            if dry_run:
                result['would_change'] += 1
                continue
            self.api.graphql(SET_STATUS, project=self.project_id, item=item['id'], field=field['id'], option=options[status])
            observed = self.api.graphql(VERIFY_ITEM, item=item['id'])['node']
            if (observed is None
                    or (observed.get('project') or {}).get('id') != self.project_id
                    or (observed.get('content') or {}).get('id') != issue['node_id']
                    or (observed.get('fieldValueByName') or {}).get('name') != status):
                raise RuntimeError('project Status update failed readback verification')
            result['changed'] += 1
        return result


def positive_number(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('issue number must be positive')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('REPOSITORY'))
    parser.add_argument('--project-id', default=os.environ.get('PROJECT_ID'))
    parser.add_argument('--issue-number', type=positive_number,
                        default=os.environ.get('ISSUE_NUMBER') or None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.repo or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', args.repo):
        parser.error('--repo must be OWNER/REPO')
    if not args.project_id or not re.fullmatch(r'PVT_[A-Za-z0-9_-]+', args.project_id):
        parser.error('--project-id must be a ProjectV2 node ID')
    numbers = [args.issue_number] if args.issue_number is not None else None
    try:
        result = Syncer(GitHub(), args.repo, args.project_id).run(numbers, args.dry_run)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(dict(result, repository=args.repo, dry_run=args.dry_run), sort_keys=True))


if __name__ == '__main__':
    main()
