import logging as log
import time
from collections import namedtuple
from tempfile import TemporaryDirectory

from . import batch_job
from . import git
from . import job
from . import merge_request as merge_request_module
from . import single_merge_job
from . import store
from .project import AccessLevel, Project

MergeRequest = merge_request_module.MergeRequest


class Bot:
    def __init__(self, *, api, config):
        self._api = api
        self._config = config

        user = config.user
        opts = config.merge_opts

        if not user.is_admin:
            assert not opts.reapprove, (
                f"{user.username} is not an admin, can't impersonate!"
            )
            assert not opts.add_reviewers, (
                f"{user.username} is not an admin, can't lookup Reviewed-by: email addresses"
            )

    def start(self):
        skip_clone = self._config.merge_opts.fusion is Fusion.gitlab_rebase
        with TemporaryDirectory() as root_dir:
            if self._config.use_only_gitlab_api:
                repo_manager = store.ApiOnlyRepoManager(
                    user=self.user,
                    root_dir=root_dir,
                    skip_clone=skip_clone,
                    timeout=self._config.git_timeout,
                    reference=self._config.git_reference_repo,
                )
            elif self._config.use_https:
                repo_manager = store.HttpsRepoManager(
                    user=self.user,
                    root_dir=root_dir,
                    skip_clone=skip_clone,
                    auth_token=self._config.auth_token,
                    timeout=self._config.git_timeout,
                    reference=self._config.git_reference_repo,
                )
            else:
                repo_manager = store.SshRepoManager(
                    user=self.user,
                    root_dir=root_dir,
                    skip_clone=skip_clone,
                    ssh_key_file=self._config.ssh_key_file,
                    timeout=self._config.git_timeout,
                    reference=self._config.git_reference_repo,
                )
            self._run(repo_manager)

    @property
    def user(self):
        return self._config.user

    @property
    def api(self):
        return self._api

    def _run(self, repo_manager):
        time_to_sleep_between_projects_in_secs = 1
        min_time_to_sleep_after_iterating_all_projects_in_secs = 30
        while True:
            projects = self._get_projects()
            processed_merge_requests = self._process_projects(
                repo_manager,
                time_to_sleep_between_projects_in_secs,
                projects,
            )
            if self._config.cli:
                return

            if processed_merge_requests == 0:
                big_sleep = max(0,
                                min_time_to_sleep_after_iterating_all_projects_in_secs
                                - time_to_sleep_between_projects_in_secs * len(projects))
                log.debug('Sleeping for %s seconds...', big_sleep)
                time.sleep(big_sleep)

    def _get_projects(self):
        log.debug('Finding out my current projects...')
        my_projects = Project.fetch_all_mine(self._api)
        project_regexp = self._config.project_regexp
        filtered_projects = [p for p in my_projects if project_regexp.match(p.path_with_namespace)]
        log.debug(
            'Projects that match project_regexp: %s',
            [p.path_with_namespace for p in filtered_projects]
        )
        filtered_out = set(my_projects) - set(filtered_projects)
        if filtered_out:
            log.debug(
                'Projects that do not match project_regexp: %s',
                [p.path_with_namespace for p in filtered_out]
            )
        return filtered_projects

    def _process_projects(
        self,
        repo_manager,
        time_to_sleep_between_projects_in_secs,
        projects,
    ):
        """Returns the number of processed merge requests."""
        processed_merge_requests = 0

        for project in projects:
            project_name = project.path_with_namespace

            if project.access_level < AccessLevel.reporter:
                log.warning("Don't have enough permissions to browse merge requests in %s!", project_name)
                continue
            merge_requests = self._get_merge_requests(project, project_name)
            processed_merge_requests += len(merge_requests)
            self._process_merge_requests(repo_manager, project, merge_requests)
            time.sleep(time_to_sleep_between_projects_in_secs)

        return processed_merge_requests

    def _get_merge_requests(self, project, project_name):
        log.info('Fetching merge requests assigned to me in %s...', project_name)
        my_merge_requests = MergeRequest.fetch_all_open_for_user(
            project_id=project.id,
            user=self.user,
            api=self._api,
            merge_order=self._config.merge_order,
        )
        branch_regexp = self._config.branch_regexp
        filtered_mrs = [mr for mr in my_merge_requests
                        if branch_regexp.match(mr.target_branch)]
        log.debug(
            'MRs that match branch_regexp: %s',
            [mr.web_url for mr in filtered_mrs]
        )
        filtered_out = set(my_merge_requests) - set(filtered_mrs)
        if filtered_out:
            log.debug(
                'MRs that do not match branch_regexp: %s',
                [mr.web_url for mr in filtered_out]
            )
        source_branch_regexp = self._config.source_branch_regexp
        source_filtered_mrs = [mr for mr in filtered_mrs
                               if source_branch_regexp.match(mr.source_branch)]
        log.debug(
            'MRs that match source_branch_regexp: %s',
            [mr.web_url for mr in source_filtered_mrs]
        )
        source_filtered_out = set(filtered_mrs) - set(source_filtered_mrs)
        if source_filtered_out:
            log.debug(
                'MRs that do not match source_branch_regexp: %s',
                [mr.web_url for mr in source_filtered_out]
            )
        return source_filtered_mrs

    def _process_merge_requests(self, repo_manager, project, merge_requests):
        if not merge_requests:
            log.debug('Nothing to merge at this point...')
            return

        try:
            repo = repo_manager.repo_for_project(project)
        except git.GitError:
            log.exception("Couldn't initialize repository for project!")
            raise

        log.info('Got %s requests to merge;', len(merge_requests))
        if self._config.batch and len(merge_requests) > 1 and not self._config.use_only_gitlab_api:
            log.info('Attempting to merge as many MRs as possible using BatchMergeJob...')
            batch_merge_job = batch_job.BatchMergeJob(
                api=self._api,
                user=self.user,
                project=project,
                merge_requests=merge_requests,
                repo=repo,
                config=self._config,
                options=self._config.merge_opts,
            )
            try:
                batch_merge_job.execute()
                return
            except batch_job.CannotBatch as err:
                log.warning('BatchMergeJob aborted: %s', err)
            except batch_job.CannotMerge as err:
                log.warning('BatchMergeJob failed: %s', err)
                return
            except git.GitError as err:
                log.exception('BatchMergeJob failed: %s', err)

        log.info('Attempting to merge the oldest MR...')
        merge_request = merge_requests[0]
        merge_job = self._get_single_job(
            project=project,
            merge_request=merge_request,
            repo=repo,
            config=self._config,
            options=self._config.merge_opts,
        )
        merge_job.execute()

    def _get_single_job(self, project, merge_request, repo, config, options):
        return single_merge_job.SingleMergeJob(
            api=self._api,
            user=self.user,
            project=project,
            merge_request=merge_request,
            repo=repo,
            config=config,
            options=options,
        )


class BotConfig(namedtuple('BotConfig',
                           'user use_https auth_token ssh_key_file project_regexp merge_order merge_opts '
                           + 'git_timeout git_reference_repo branch_regexp source_branch_regexp batch cli '
                           + 'use_only_gitlab_api')):
    pass


MergeJobOptions = job.MergeJobOptions
Fusion = job.Fusion
