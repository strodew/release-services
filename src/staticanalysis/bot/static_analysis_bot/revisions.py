# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import io
import os
import re
from collections import OrderedDict
from datetime import timedelta

import hglib
from parsepatch.patch import Patch

from cli_common import log
from cli_common.phabricator import PhabricatorAPI
from cli_common.taskcluster import create_blob_artifact
from static_analysis_bot import AnalysisException
from static_analysis_bot import Issue
from static_analysis_bot import stats
from static_analysis_bot.config import settings

logger = log.get_logger(__name__)


def revision_available(repo, revision):
    '''
    Check a revision is available on a Mercurial repo
    '''
    try:
        repo.identify(revision)
        return True
    except hglib.error.CommandError:
        return False


class ImprovementPatch(object):
    '''
    An improvement patch built by the bot
    '''
    def __init__(self, analyzer_name, patch_name, content):
        # Build name from analyzer and revision
        self.analyzer = analyzer_name
        self.name = '{}-{}.diff'.format(analyzer_name, patch_name)
        self.content = content
        self.url = None
        self.path = None

    def __str__(self):
        return '{}: {}'.format(self.analyzer, self.url or self.path or self.name)

    def write(self):
        '''
        Write patch on local FS, for dev & tests only
        '''
        self.path = os.path.join(settings.taskcluster.results_dir, self.name)
        with open(self.path, 'w') as f:
            length = f.write(self.content)
            logger.info('Improvement patch saved', path=self.path, length=length)

    def publish(self, queue_service, days_ttl=30):
        '''
        Push through Taskcluster API to setup the content-type header
        so it displays nicely in browsers
        '''
        assert not settings.taskcluster.local, 'Only publish on online Taskcluster tasks'
        self.url = create_blob_artifact(
            queue_service,
            task_id=settings.taskcluster.task_id,
            run_id=settings.taskcluster.run_id,
            path='public/patch/{}'.format(self.name),
            content=self.content,
            content_type='text/plain; charset=utf-8',  # Displays instead of download):
            ttl=timedelta(days=days_ttl - 1, hours=23),
        )
        logger.info('Improvement patch published', url=self.url)


class Revision(object):
    '''
    A common DCM revision
    '''
    def __init__(self):
        self.files = []
        self.lines = {}
        self.patch = None
        self.improvement_patches = []

    def analyze_patch(self):
        '''
        Analyze loaded patch to extract modified lines
        and statistics
        '''
        assert self.patch is not None, \
            'Missing patch'
        assert isinstance(self.patch, str), \
            'Invalid patch type'

        # List all modified lines from current revision changes
        patch = Patch.parse_patch(self.patch, skip_comments=False)
        assert patch != {}, \
            'Empty patch'
        self.lines = {
            # Use all changes in new files
            filename: diff.get('touched', []) + diff.get('added', [])
            for filename, diff in patch.items()
        }

        # Shortcut to files modified
        self.files = self.lines.keys()

        # Report nb of files and lines analyzed
        stats.api.increment('analysis.files', len(self.files))
        stats.api.increment('analysis.lines', sum(len(line) for line in self.lines.values()))

    def contains(self, issue):
        '''
        Check if the issue is this patch
        '''
        assert isinstance(issue, Issue)

        # Get modified lines for this issue
        modified_lines = self.lines.get(issue.path)
        if modified_lines is None:
            logger.warn('Issue path in not in revision', path=issue.path, revision=self)
            return False

        # Detect if this issue is in the patch
        lines = set(range(issue.line, issue.line + issue.nb_lines))
        return not lines.isdisjoint(modified_lines)

    @property
    def has_clang_files(self):
        '''
        Check if this revision has any file that might
        be a C/C++ file
        '''
        def _is_clang(filename):
            _, ext = os.path.splitext(filename)
            return ext.lower() in settings.cpp_extensions

        return any(_is_clang(f) for f in self.files)

    @property
    def has_infer_files(self):
        '''
        Check if this revision has any file that might
        be a Java file
        '''
        def _is_infer(filename):
            _, ext = os.path.splitext(filename)
            return ext.lower() in settings.java_extensions

        return any(_is_infer(f) for f in self.files)

    def add_improvement_patch(self, analyzer_name, content):
        '''
        Save an improvement patch, and make it available
        as a Taskcluster artifact
        '''
        assert isinstance(content, str)
        assert len(content) > 0
        self.improvement_patches.append(
            ImprovementPatch(analyzer_name, repr(self), content)
        )


class PhabricatorRevision(Revision):
    '''
    A phabricator revision to process
    '''
    regex = re.compile(r'^(PHID-DIFF-(?:\w+))$')

    def __init__(self, description, api):
        super().__init__()
        assert isinstance(api, PhabricatorAPI)
        self.api = api

        # Parse Diff description
        match = self.regex.match(description)
        if match is None:
            raise Exception('Invalid Phabricator description')
        groups = match.groups()
        self.diff_phid = groups[0]

        # Load diff details to get the diff revision
        diffs = self.api.search_diffs(diff_phid=self.diff_phid)
        assert len(diffs) == 1, 'No diff available for {}'.format(self.diff_phid)
        self.diff = diffs[0]
        self.diff_id = self.diff['id']
        self.phid = self.diff['revisionPHID']

        self.revision = self.api.load_revision(self.phid)
        self.id = self.revision['id']

    @property
    def namespaces(self):
        return [
            'phabricator.{}'.format(self.id),
            'phabricator.diff.{}'.format(self.diff_id),
            'phabricator.phid.{}'.format(self.phid),
            'phabricator.diffphid.{}'.format(self.diff_phid),
        ]

    def __repr__(self):
        return self.diff_phid

    def __str__(self):
        return 'Phabricator #{} - {}'.format(self.diff_id, self.diff_phid)

    @property
    def url(self):
        return 'https://{}/D{}'.format(self.api.hostname, self.id)

    def load(self, repo):
        '''
        Load full stack of patches from Phabricator:
        * setup repo to base revision from Mozilla Central
        * Apply previous needed patches from Phabricator
        '''
        assert isinstance(repo, hglib.client.hgclient)

        # Diff PHIDs from our patch to its base
        patches = OrderedDict()
        patches[self.diff_phid] = self.diff_id

        parents = self.api.load_parents(self.phid)
        if parents:

            # Load all parent diffs
            for parent in parents:
                logger.info('Loading parent diff', phid=parent)

                # Sort parent diffs by their id to load the most recent patch
                parent_diffs = sorted(
                    self.api.search_diffs(revision_phid=parent),
                    key=lambda x: x['id'],
                )
                last_diff = parent_diffs[-1]
                patches[last_diff['phid']] = last_diff['id']

                # Use base revision of last parent
                hg_base = last_diff['baseRevision']

        else:
            # Use base revision from top diff
            hg_base = self.diff['baseRevision']

        # When base revision is missing, update to top of Central
        if hg_base is None or not revision_available(repo, hg_base):
            logger.warning('Missing base revision from Phabricator')
            hg_base = 'central'

        # Load all patches from their numerical ID
        for diff_phid, diff_id in patches.items():
            patches[diff_phid] = self.api.load_raw_diff(diff_id)

        # Expose current patch to workflow
        self.patch = patches[self.diff_phid]

        # Update the repo to base revision
        try:
            logger.info('Updating repo to revision', rev=hg_base)
            repo.update(
                rev=hg_base,
                clean=True,
            )
        except hglib.error.CommandError:
            raise AnalysisException('mercurial', 'Failed to update to revision {}'.format(hg_base))

        # Apply all patches from base to top
        # except our current (top) patch
        for diff_phid, patch in reversed(list(patches.items())[1:]):
            logger.info('Applying parent diff', phid=diff_phid)
            try:
                repo.import_(
                    patches=io.BytesIO(patch.encode('utf-8')),
                    message='SA Imported patch {}'.format(diff_phid),
                    user='reviewbot',
                )
            except hglib.error.CommandError:
                raise AnalysisException('mercurial', 'Failed to import parent patch {}'.format(diff_phid))

    def apply(self, repo):
        '''
        Apply patch from Phabricator to Mercurial local repository
        '''
        assert isinstance(repo, hglib.client.hgclient)

        # Apply the patch on top of repository
        try:
            repo.import_(
                patches=io.BytesIO(self.patch.encode('utf-8')),
                message='SA Analyzed patch',
                user='reviewbot',
            )
            logger.info('Applied target patch', phid=self.diff_phid)
        except hglib.error.CommandError:
            raise AnalysisException('mercurial', 'Failed to import target patch')

    def as_dict(self):
        '''
        Outputs a serializable representation of this revision
        '''
        return {
            'source': 'phabricator',
            'diff_phid': self.diff_phid,
            'phid': self.phid,
            'id': self.id,
            'url': self.url,
            'has_clang_files': self.has_clang_files,

            # Extra infos for frontend
            'title': self.revision['fields'].get('title'),
            'bugzilla_id': self.revision['fields'].get('bugzilla.bug-id'),
        }
