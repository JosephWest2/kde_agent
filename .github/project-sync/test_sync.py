import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent


class CommandTests(unittest.TestCase):
    def test_help_is_available_without_a_token(self):
        result = subprocess.run(
            [sys.executable, str(HERE / 'sync.py'), '--help'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--project-id', result.stdout)
        self.assertIn('--dry-run', result.stdout)


class StatusTests(unittest.TestCase):
    def test_state_labels_and_pr_defaults_choose_status(self):
        import sync
        self.assertTrue(hasattr(sync, 'select_status'), 'status policy is missing')
        cases = [
            ({'state': 'open', 'labels': []}, 'Todo'),
            ({'state': 'closed', 'labels': [{'name': 'blocked'}]}, 'Done'),
            ({'state': 'open', 'labels': [{'name': 'backlog'}]}, 'Backlog'),
            ({'state': 'open', 'labels': [{'name': 'review'}]}, 'Review'),
            ({'state': 'open', 'labels': [{'name': 'in-progress'}]}, 'In progress'),
            ({'state': 'open', 'labels': [{'name': 'blocked'}, {'name': 'in-progress'}, {'name': 'review'}, {'name': 'backlog'}]}, 'Blocked'),
            ({'state': 'open', 'labels': [{'name': 'backlog'}, {'name': 'in-progress'}]}, 'In progress'),
            ({'state': 'open', 'labels': [], 'pull_request': {}, 'draft': True}, 'In progress'),
            ({'state': 'open', 'labels': [], 'pull_request': {}, 'draft': False}, 'Review'),
            ({'state': 'open', 'labels': [{'name': 'blocked'}], 'pull_request': {}, 'draft': False}, 'Blocked'),
        ]
        for issue, expected in cases:
            with self.subTest(issue=issue):
                self.assertEqual(sync.select_status(issue), expected)
        with self.assertRaises(ValueError):
            sync.select_status({'state': 'unknown', 'labels': []})


class MemoryGitHub:
    """API fixture; no requests reach GitHub during unit tests."""
    repo = 'owner/repo'
    project_id = 'PVT_fixture'

    def __init__(self):
        self.issues = {1: {'number': 1, 'node_id': 'ISSUE_1', 'state': 'closed', 'labels': []}}
        self.items = {'ITEM_1': {'id': 'ITEM_1', 'isArchived': False,
                      'content': {'id': 'ISSUE_1', 'number': 1, 'repository': {'nameWithOwner': self.repo}},
                      'fieldValueByName': {'name': 'Todo'}}}
        self.mutations = []
        self.operations = []
        self.linked = True
        self.options = ['Backlog', 'Todo', 'In progress', 'Review', 'Blocked', 'Done']

    def rest(self, path, paginate=False):
        import copy
        self.operations.append(('REST', path))
        if paginate:
            return copy.deepcopy([i for i in self.issues.values() if i['state'] == 'open'])
        return copy.deepcopy(self.issues[int(path.rsplit('/', 1)[-1])])

    def graphql(self, query, **variables):
        import copy
        self.operations.append(('GraphQL', query.split('{', 1)[0]))
        if 'query ProjectInfo' in query:
            return {'node': {'id': self.project_id,
                    'repositories': {'pageInfo': {'hasNextPage': False}, 'nodes': [{'nameWithOwner': self.repo}] if self.linked else []},
                    'fields': {'pageInfo': {'hasNextPage': False}, 'nodes': [
                        {'id': 'STATUS', 'name': 'Status', 'options': [{'id': 'o-' + n, 'name': n} for n in self.options]}]}}}
        if 'query ProjectItems' in query:
            # GitHub defaults to NOT_ARCHIVED when no explicit states are supplied.
            include_archived = 'archivedStates: [ARCHIVED, NOT_ARCHIVED]' in query
            nodes = [i for i in self.items.values() if include_archived or not i['isArchived']]
            return {'node': {'items': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                    'nodes': copy.deepcopy(nodes)}}}
        if 'mutation AddItem' in query:
            issue = next(i for i in self.issues.values() if i['node_id'] == variables['content'])
            item_id = 'ITEM_' + str(issue['number'])
            # An idempotent add returns existing membership without overwriting it.
            if item_id not in self.items:
                self.items[item_id] = {'id': item_id, 'isArchived': False,
                        'content': {'id': issue['node_id'], 'number': issue['number'], 'repository': {'nameWithOwner': self.repo}},
                        'fieldValueByName': None}
                self.mutations.append('add')
            response = {'id': item_id}
            if 'isArchived' in query:
                response['isArchived'] = self.items[item_id]['isArchived']
            return {'addProjectV2ItemById': {'item': response}}
        if 'mutation SetStatus' in query:
            self.items[variables['item']]['fieldValueByName'] = {'name': variables['option'][2:]}
            self.mutations.append('status')
            return {'updateProjectV2ItemFieldValue': {'projectV2Item': {'id': variables['item']}}}
        if 'query VerifyItem' in query:
            item = copy.deepcopy(self.items[variables['item']])
            item['project'] = {'id': self.project_id}
            return {'node': item}
        raise AssertionError('Unexpected API operation: ' + query)


class SyncTests(unittest.TestCase):
    def test_closed_issue_moves_to_done_and_is_read_back(self):
        import sync
        self.assertTrue(hasattr(sync, 'Syncer'), 'project synchronization is missing')
        api = MemoryGitHub()
        before = dict(api.issues[1])
        result = sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'Done')
        self.assertEqual(result['changed'], 1)
        self.assertEqual(api.issues[1], before)
        self.assertTrue(any('query VerifyItem' in q for kind, q in api.operations if kind == 'GraphQL'))
    def test_new_open_issue_is_added_and_verified(self):
        import sync
        api = MemoryGitHub()
        api.issues[2] = {'number': 2, 'node_id': 'ISSUE_2', 'state': 'open', 'labels': [{'name': 'blocked'}]}
        try:
            result = sync.Syncer(api, api.repo, api.project_id).run([2])
        except KeyError:
            self.fail('new issues are not imported into the project')
        self.assertEqual(api.items['ITEM_2']['fieldValueByName']['name'], 'Blocked')
        self.assertEqual(result['added'], 1)
        self.assertEqual(api.mutations, ['add', 'status'])

    def test_reopen_and_label_removal_follow_current_issue_not_old_event(self):
        import sync
        api = MemoryGitHub()
        api.issues[1]['state'] = 'open'
        api.items['ITEM_1']['fieldValueByName'] = {'name': 'Done'}
        sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'Todo')
        api.issues[1]['labels'] = [{'name': 'blocked'}]
        sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'Blocked')
        api.issues[1]['labels'] = []
        sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'Todo')
        api.mutations.clear()
        result = sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(api.mutations, [])


    def test_full_dry_run_plans_new_open_and_existing_closed_items_only(self):
        import inspect
        import sync
        self.assertIn('dry_run', inspect.signature(sync.Syncer.run).parameters, 'dry-run is missing')
        api = MemoryGitHub()
        api.issues[2] = {'number': 2, 'node_id': 'ISSUE_2', 'state': 'open', 'labels': [{'name': 'blocked'}]}
        api.issues[3] = {'number': 3, 'node_id': 'ISSUE_3', 'state': 'closed', 'labels': []}
        result = sync.Syncer(api, api.repo, api.project_id).run(dry_run=True)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['would_add'], 1)
        self.assertEqual(result['would_change'], 2)
        self.assertEqual(api.mutations, [])
        self.assertNotIn('ITEM_3', api.items)
        result = sync.Syncer(api, api.repo, api.project_id).run()
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['changed'], 2)
        self.assertNotIn('ITEM_3', api.items)


    def test_archived_items_are_not_changed_or_unarchived(self):
        import sync
        api = MemoryGitHub()
        api.items['ITEM_1']['isArchived'] = True
        result = sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.mutations, [])
        self.assertTrue(api.items['ITEM_1']['isArchived'])
        self.assertEqual(result['skipped'], 1)


    def test_missing_status_options_fail_before_any_write(self):
        import sync
        api = MemoryGitHub()
        api.options.remove('Blocked')
        with self.assertRaisesRegex(ValueError, 'missing'):
            sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.mutations, [])
        api = MemoryGitHub()
        api.linked = False
        with self.assertRaises(ValueError):
            sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.mutations, [])


    def test_draft_pr_state_is_read_from_the_pull_request_endpoint(self):
        import sync
        api = MemoryGitHub()
        api.issues[1] = {'number': 1, 'node_id': 'ISSUE_1', 'state': 'open', 'labels': [], 'pull_request': {}}
        original = api.rest
        def rest(path, paginate=False):
            if '/pulls/' in path:
                return {'draft': True}
            return original(path, paginate)
        api.rest = rest
        try:
            sync.Syncer(api, api.repo, api.project_id).run([1])
        except KeyError:
            self.fail('PR draft status was not refreshed')
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'In progress')


class TransportTests(unittest.TestCase):
    def test_transport_paginates_and_surfaces_api_failures(self):
        import json
        from unittest.mock import patch
        import sync
        self.assertTrue(hasattr(sync, 'GitHub'), 'GitHub transport is missing')
        api = sync.GitHub()
        response = subprocess.CompletedProcess([], 0, json.dumps([[{'number': 1}], [{'number': 2}]]), '')
        with patch('sync.subprocess.run', return_value=response) as run:
            self.assertEqual(api.rest('repos/owner/repo/issues', paginate=True), [{'number': 1}, {'number': 2}])
            self.assertIn('--paginate', run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)
        response = subprocess.CompletedProcess([], 0, json.dumps({'data': {'node': {'id': 'ID'}}}), '')
        with patch('sync.subprocess.run', return_value=response) as run:
            self.assertEqual(api.graphql('query Q($id: ID!){node(id:$id){id}}', id='ID'), {'node': {'id': 'ID'}})
            payload = json.loads(run.call_args.kwargs['input'])
            self.assertEqual(payload['variables'], {'id': 'ID'})
        response = subprocess.CompletedProcess([], 0, json.dumps({'errors': [{'message': 'denied'}]}), '')
        with patch('sync.subprocess.run', return_value=response):
            with self.assertRaises(RuntimeError):
                api.graphql('query Q{viewer{login}}')
        response = subprocess.CompletedProcess([], 1, '', 'denied')
        with patch('sync.subprocess.run', return_value=response):
            with self.assertRaises(RuntimeError):
                api.rest('repos/owner/repo/issues')


class MainTests(unittest.TestCase):
    def test_cli_runs_sync_and_rejects_unsafe_repository_names(self):
        import io
        import json
        from unittest.mock import patch
        import sync
        api = MemoryGitHub()
        output = io.StringIO()
        argv = ['sync.py', '--repo', api.repo, '--project-id', api.project_id, '--issue-number', '1']
        with patch('sync.GitHub', return_value=api), patch.object(sys, 'argv', argv), patch('sys.stdout', output):
            sync.main()
        self.assertIn('processed', output.getvalue(), 'CLI does not execute synchronization')
        self.assertEqual(json.loads(output.getvalue())['changed'], 1)
        with patch.object(sys, 'argv', ['sync.py', '--repo', '../wrong', '--project-id', api.project_id]), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                sync.main()
            self.assertEqual(result.exception.code, 2)
        with patch.object(sys, 'argv', ['sync.py', '--repo', api.repo, '--project-id', api.project_id, '--issue-number', '-1']), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                sync.main()
            self.assertEqual(result.exception.code, 2)


class RaceTests(unittest.TestCase):
    def test_concurrent_membership_add_is_reused_instead_of_failing(self):
        import sync
        api = MemoryGitHub()
        api.issues[2] = {'number': 2, 'node_id': 'ISSUE_2', 'state': 'open', 'labels': []}
        original = api.graphql
        def graphql(query, **variables):
            if 'mutation AddItem' in query:
                original(query, **variables)
                raise RuntimeError('Content already exists in this project')
            return original(query, **variables)
        api.graphql = graphql
        try:
            result = sync.Syncer(api, api.repo, api.project_id).run([2])
        except RuntimeError as error:
            self.fail('concurrent addition is not handled: ' + str(error))
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['changed'], 1)
        self.assertEqual(api.items['ITEM_2']['fieldValueByName']['name'], 'Todo')


class BoundaryTests(unittest.TestCase):
    def test_project_paging_keeps_repo_scope_and_continues_cursor(self):
        import sync
        api = MemoryGitHub()
        original = api.graphql
        cursors = []
        def graphql(query, **variables):
            if 'query ProjectItems' in query:
                cursors.append(variables['cursor'])
                if variables['cursor'] is None:
                    return {'node': {'items': {'nodes': [
                        {'id': 'FOREIGN', 'content': {'number': 9, 'repository': {'nameWithOwner': 'other/repo'}}},
                        {'id': 'DELETED', 'content': None}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'page2'}}}}
            return original(query, **variables)
        api.graphql = graphql
        result = sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(cursors, [None, 'page2'])
        self.assertEqual(result['changed'], 1)

    def test_mismatched_write_readback_is_a_failure(self):
        import sync
        api = MemoryGitHub()
        original = api.graphql
        def graphql(query, **variables):
            result = original(query, **variables)
            if 'query VerifyItem' in query:
                result['node']['fieldValueByName'] = {'name': 'Todo'}
            return result
        api.graphql = graphql
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            sync.Syncer(api, api.repo, api.project_id).run([1])


class ArchiveTests(unittest.TestCase):
    def test_archived_open_issue_is_skipped_during_full_scan(self):
        import sync
        api = MemoryGitHub()
        api.issues[1]['state'] = 'open'
        api.items['ITEM_1']['isArchived'] = True
        api.items['ITEM_1']['fieldValueByName'] = {'name': 'Backlog'}
        result = sync.Syncer(api, api.repo, api.project_id).run()
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(api.mutations, [])
        self.assertEqual(api.items['ITEM_1']['fieldValueByName']['name'], 'Backlog')

    def test_idempotent_add_returning_concurrently_archived_item_is_skipped(self):
        import sync
        api = MemoryGitHub()
        api.issues[2] = {'number': 2, 'node_id': 'ISSUE_2', 'state': 'open', 'labels': []}
        original = api.rest
        def rest(path, paginate=False):
            result = original(path, paginate)
            if path.endswith('/issues/2'):
                api.items['ITEM_2'] = {'id': 'ITEM_2', 'isArchived': True,
                        'content': {'id': 'ISSUE_2', 'number': 2, 'repository': {'nameWithOwner': api.repo}},
                        'fieldValueByName': {'name': 'Backlog'}}
            return result
        api.rest = rest
        result = sync.Syncer(api, api.repo, api.project_id).run([2])
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(api.mutations, [])
        self.assertEqual(api.items['ITEM_2']['fieldValueByName']['name'], 'Backlog')


class MissingFieldTests(unittest.TestCase):
    def test_missing_status_field_fails_before_writes(self):
        import sync
        api = MemoryGitHub()
        original = api.graphql
        def graphql(query, **variables):
            result = original(query, **variables)
            if 'query ProjectInfo' in query:
                result['node']['fields']['nodes'] = []
            return result
        api.graphql = graphql
        try:
            with self.assertRaisesRegex(ValueError, 'Status'):
                sync.Syncer(api, api.repo, api.project_id).run([1])
        except StopIteration:
            self.fail('missing Status field has no concise failure diagnostic')
        self.assertEqual(api.mutations, [])


class TruncationTests(unittest.TestCase):
    def test_truncated_linked_repository_list_fails_before_writes(self):
        import sync
        api = MemoryGitHub()
        original = api.graphql
        def graphql(query, **variables):
            result = original(query, **variables)
            if 'query ProjectInfo' in query:
                result['node']['repositories']['pageInfo']['hasNextPage'] = True
            return result
        api.graphql = graphql
        with self.assertRaisesRegex(ValueError, 'repository list'):
            sync.Syncer(api, api.repo, api.project_id).run([1])
        self.assertEqual(api.mutations, [])


class NullReadbackTests(unittest.TestCase):
    def test_null_readback_is_an_explicit_verification_failure(self):
        import sync
        api = MemoryGitHub()
        original = api.graphql
        def graphql(query, **variables):
            if 'query VerifyItem' in query:
                return {'node': None}
            return original(query, **variables)
        api.graphql = graphql
        try:
            with self.assertRaisesRegex(RuntimeError, 'readback'):
                sync.Syncer(api, api.repo, api.project_id).run([1])
        except TypeError:
            self.fail('null readback has no concise failure diagnostic')


if __name__ == '__main__':
    unittest.main()
