"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from typing import TypedDict, List, Dict, Any, Optional

from osbs.repo_utils import ModuleSpec

from atomic_reactor import util
from atomic_reactor.config import get_koji_session, get_odcs_session
from atomic_reactor.constants import (PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY,
                                      BASE_IMAGE_KOJI_BUILD)
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import get_platforms, is_isolated_build, is_scratch_build
from atomic_reactor.utils.odcs import WaitComposeToFinishTimeout

ODCS_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MINIMUM_TIME_TO_EXPIRE = timedelta(hours=2).total_seconds()
# flag to let ODCS see hidden pulp repos
UNPUBLISHED_REPOS = 'include_unpublished_pulp_repos'
# flag to let ODCS ignore missing content sets
IGNORE_ABSENT_REPOS = 'ignore_absent_pulp_repos'


class ResolveComposesResult(TypedDict):
    composes: List[Dict[str, Any]]
    # list of repourls per platform
    yum_repourls: Dict[str, List[str]]
    # include koji repo for platform
    include_koji_repo: Dict[str, bool]
    signing_intent: Optional[str]
    signing_intent_overridden: bool


class ResolveComposesPlugin(Plugin):
    """Request a new, or use existing, ODCS compose

    This plugin will read the configuration in git repository
    and request ODCS to create a corresponding yum repository.
    """

    key = PLUGIN_RESOLVE_COMPOSES_KEY
    is_allowed_to_fail = False

    args_from_user_params = util.map_to_user_params(
        "koji_target",
        "signing_intent",
        "compose_ids",
        "repourls:yum_repourls",
    )

    def __init__(self, workflow, koji_target=None, signing_intent=None, compose_ids=tuple(),
                 repourls=None, minimum_time_to_expire=MINIMUM_TIME_TO_EXPIRE):
        """
        :param workflow: DockerBuildWorkflow instance
        :param koji_target: str, koji target contains build tag to be used
                            when requesting compose from "tag"
        :param signing_intent: override the signing intent from git repo configuration
        :param compose_ids: use the given compose_ids instead of requesting a new one
        :param repourls: list of str, URLs to the repo files
        :param minimum_time_to_expire: int, used in deciding when to extend compose's time
                                       to expire in seconds
        """
        super(ResolveComposesPlugin, self).__init__(workflow)

        if signing_intent and compose_ids:
            raise ValueError('signing_intent and compose_ids cannot be used at the same time')

        self.signing_intent = signing_intent
        self.compose_ids = compose_ids
        self.koji_target = koji_target
        self.minimum_time_to_expire = minimum_time_to_expire

        self._koji_session = None
        self._odcs_client = None
        self.odcs_config = None
        self.compose_config = None
        self.composes_info = []
        self._parent_signing_intent = None
        self.repourls = repourls or []
        self.plugin_result = self.workflow.data.plugins_results.get(PLUGIN_KOJI_PARENT_KEY)
        self.all_compose_ids = list(self.compose_ids)
        self.new_compose_ids = []
        self.parent_compose_ids = []
        self.yum_repourls = defaultdict(list)
        self.platforms = get_platforms(self.workflow.data)

    def run(self) -> ResolveComposesResult:
        if self.allow_inheritance():
            self.adjust_for_inherit()
        self.workflow.data.all_yum_repourls = self.repourls

        try:
            self.read_configs()
        except SkipResolveComposesPlugin as abort_exc:
            self.log.info('Aborting plugin execution: %s', abort_exc)
            for arch in self.platforms:
                self.yum_repourls[arch].extend(self.repourls)
            return self.make_result()

        self.adjust_compose_config()
        self.request_compose_if_needed()
        try:
            self.wait_for_composes()
        except WaitComposeToFinishTimeout as e:
            self.log.info(str(e))

            for compose_id in self.new_compose_ids:
                if self.odcs_client.get_compose_status(compose_id) in ['wait', 'generating']:
                    self.log.info('Canceling the compose %s', compose_id)
                    self.odcs_client.cancel_compose(compose_id)
                else:
                    self.log.info('The compose %s is not in progress, skip canceling', compose_id)
            raise
        self.resolve_signing_intent()
        self.forward_composes()
        return self.make_result()

    def allow_inheritance(self):
        """Returns boolean if composes can be inherited"""
        if not self.workflow.source.config.inherit:
            return False
        self.log.info("Inheritance requested in container.yaml file")

        if is_scratch_build(self.workflow) or is_isolated_build(self.workflow):
            msg = ("'inherit: true' in the compose section of container.yaml "
                   "is not allowed for scratch or isolated builds. "
                   "Skipping inheritance.")
            self.log.warning(msg)
            self.log.user_warning(message=msg)
            return False

        return True

    def adjust_for_inherit(self):
        if self.workflow.data.dockerfile_images.base_from_scratch:
            self.log.debug('This is a base image based on scratch. '
                           'Skipping adjusting composes for inheritance.')
            return

        if not self.plugin_result:
            return

        build_info = self.plugin_result.get(BASE_IMAGE_KOJI_BUILD)
        if not build_info:
            self.log.warning('Parent koji build does not exist can not inherit from the parent')
            return
        parent_repourls = []

        try:
            self.parent_compose_ids = build_info['extra']['image']['odcs']['compose_ids']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define compose_ids.'
                           'Cannot add compose_ids for inheritance from parent.',
                           build_info['nvr'], build_info['id'])
        try:
            parent_repourls = build_info['extra']['image']['yum_repourls']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define yum_repourls. '
                           'Cannot add yum_repourls for inheritance from parent.',
                           build_info['nvr'], build_info['id'])

        all_compose_ids = set(self.compose_ids)
        original_compose_ids = deepcopy(all_compose_ids)
        all_compose_ids.update(self.parent_compose_ids)
        self.all_compose_ids = list(all_compose_ids)
        for compose_id in all_compose_ids:
            if compose_id not in original_compose_ids:
                self.log.info('Inheriting compose id %s', compose_id)

        all_yum_repos = set(self.repourls)
        original_yum_repos = deepcopy(all_yum_repos)
        all_yum_repos.update(parent_repourls)
        self.repourls = list(all_yum_repos)
        for repo in all_yum_repos:
            if repo not in original_yum_repos:
                self.log.info('Inheriting yum repo %s', repo)

    def read_configs(self):
        self.odcs_config = self.workflow.conf.odcs_config
        if not self.odcs_config:
            raise SkipResolveComposesPlugin('ODCS config not found')

        data = self.workflow.source.config.compose
        if not data and not self.all_compose_ids:
            raise SkipResolveComposesPlugin('"compose" config not set and compose_ids not given')

        pulp_data = util.read_content_sets(self.workflow) or {}

        platforms = sorted(self.platforms)  # sorted to keep predictable for tests

        koji_tag = None
        if self.koji_target:
            target_info = self.koji_session.getBuildTarget(self.koji_target, strict=True)
            koji_tag = target_info['build_tag_name']

        self.compose_config = ComposeConfig(data, pulp_data, self.odcs_config, koji_tag=koji_tag,
                                            arches=platforms)

    def adjust_compose_config(self):
        if self.signing_intent:
            self.compose_config.set_signing_intent(self.signing_intent)

        self.adjust_signing_intent_from_parent()

    def adjust_signing_intent_from_parent(self):
        if self.workflow.data.dockerfile_images.base_from_scratch:
            self.log.debug('This is a base image based on scratch. '
                           'Signing intent will not be adjusted for it.')
            return

        if not self.plugin_result:
            self.log.debug("%s plugin didn't run, signing intent will not be adjusted",
                           PLUGIN_KOJI_PARENT_KEY)
            return

        build_info = self.plugin_result.get(BASE_IMAGE_KOJI_BUILD)
        if not build_info:
            self.log.warning('Parent koji build does not exist can not adjust '
                             'signing intent from the parent')
            return

        try:
            parent_signing_intent_name = build_info['extra']['image']['odcs']['signing_intent']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define signing_intent. '
                           'Cannot adjust for current build.',
                           build_info['nvr'], build_info['id'])
            return

        self._parent_signing_intent = (self.odcs_config
                                       .get_signing_intent_by_name(parent_signing_intent_name))

        current_signing_intent = self.compose_config.signing_intent

        # Calculate the least restrictive signing intent
        new_signing_intent = min(self._parent_signing_intent, current_signing_intent,
                                 key=lambda x: x['restrictiveness'])

        if new_signing_intent != current_signing_intent:
            self.log.info('Signing intent downgraded to "%s" to match Koji parent build',
                          new_signing_intent['name'])
            self.compose_config.set_signing_intent(new_signing_intent['name'])

    def request_compose_if_needed(self):
        if self.compose_ids:
            self.log.debug('ODCS compose not requested, using given compose IDs')
            return

        if not self.workflow.source.config.compose:
            self.log.debug('ODCS compose not provided, using parents compose IDs')
            return

        self.compose_config.validate_for_request()

        for compose_request in self.compose_config.render_requests():
            compose_info = self.odcs_client.start_compose(**compose_request)
            self.new_compose_ids.append(compose_info['id'])
        self.all_compose_ids.extend(self.new_compose_ids)

    def wait_for_composes(self):
        self.log.debug('Waiting for ODCS composes to be available: %s', self.all_compose_ids)
        self.composes_info = []
        for compose_id in self.all_compose_ids:
            compose_info = self.odcs_client.wait_for_compose(compose_id)

            if self._needs_renewal(compose_info):
                sigkeys = compose_info.get('sigkeys', '').split()
                updated_signing_intent = self.odcs_config.get_signing_intent_by_keys(sigkeys)
                if set(sigkeys) != set(updated_signing_intent['keys']):
                    self.log.info('Updating signing keys in "%s" from "%s", to "%s" in compose '
                                  '"%s" due to sigkeys deprecation',
                                  updated_signing_intent['name'],
                                  sigkeys,
                                  updated_signing_intent['keys'],
                                  compose_info['id']
                                  )
                    sigkeys = updated_signing_intent['keys']

                compose_info = self.odcs_client.renew_compose(compose_id, sigkeys)
                compose_id = compose_info['id']
                self.new_compose_ids.append(compose_id)
                compose_info = self.odcs_client.wait_for_compose(compose_id)

            self.composes_info.append(compose_info)

        self.all_compose_ids = [item['id'] for item in self.composes_info]

    def _needs_renewal(self, compose_info):
        if compose_info['state_name'] == 'removed':
            return True

        time_to_expire = datetime.strptime(compose_info['time_to_expire'],
                                           ODCS_DATETIME_FORMAT)
        now = datetime.utcnow()
        seconds_left = (time_to_expire - now).total_seconds()
        return seconds_left <= self.minimum_time_to_expire

    def resolve_signing_intent(self):
        """Determine the correct signing intent

        Regardless of what was requested, or provided as signing_intent plugin parameter,
        consult sigkeys of the actual composes used to guarantee information accuracy.
        """

        all_signing_intents = [
            self.odcs_config.get_signing_intent_by_keys(compose_info.get('sigkeys', []))
            for compose_info in self.composes_info
        ]

        # Because composes_info may contain composes that were passed as
        # plugin parameters, add the parent signing intent to avoid the
        # overall signing intent from surpassing parent's.
        if self._parent_signing_intent:
            all_signing_intents.append(self._parent_signing_intent)

        # Calculate the least restrictive signing intent
        signing_intent = min(all_signing_intents, key=lambda x: x['restrictiveness'])

        self.log.info('Signing intent for build is %s', signing_intent['name'])
        self.compose_config.set_signing_intent(signing_intent['name'])

    def forward_composes(self):
        for compose_info in self.composes_info:
            result_repofile = compose_info['result_repofile']
            try:
                arches = compose_info['arches']
            except KeyError:
                self.yum_repourls['noarch'].append(result_repofile)
            else:
                for arch in arches.split():
                    self.yum_repourls[arch].append(result_repofile)

        # we should almost never have a None entry from composes,
        # but we can have yum_repos added, so if we do, we need to merge
        # it with all other repos.
        self.yum_repourls['noarch'].extend(self.repourls)
        if 'noarch' in self.yum_repourls:
            noarch_repos = self.yum_repourls.pop('noarch')
            for arch in self.yum_repourls:
                self.yum_repourls[arch].extend(noarch_repos)

    def has_complete_repos(self, platform: str) -> bool:
        # repourls are for all platforms
        if self.repourls:
            return True

        # A module compose is not standalone - it depends on packages from the
        # virtual platform module - if no extra repourls or other composes are
        # provided, we'll need packages from the target build tag.

        # We assume other types of composes might provide all the packages needed -
        # though we don't really know that for sure - a compose with packages
        # listed might list all the packages that are needed, or might also require
        # packages from some other source.
        return any(
            # any compose for this platform is a non-module compose
            compose_info['source_type'] != 2  # PungiSourceType.MODULE
            for compose_info in self.composes_info
            # missing 'arches' => compose for all arches
            if ('arches' not in compose_info) or (platform in compose_info['arches'].split())
        )

    def make_result(self) -> ResolveComposesResult:
        signing_intent = None
        signing_intent_overridden = False
        if self.compose_config:
            signing_intent = self.compose_config.signing_intent['name']
            signing_intent_overridden = self.compose_config.has_signing_intent_changed()

        result: ResolveComposesResult = {
            'composes': self.composes_info,
            'yum_repourls': self.yum_repourls,
            # If we don't think the set of packages available from the user-supplied repourls,
            # inherited repourls, and composed repositories is complete,
            # set the 'include_koji_repo' to True, so it can be
            # properly processed in inject_yum_repos plugin
            'include_koji_repo': {
                platform: not self.has_complete_repos(platform)
                for platform in self.platforms
            },
            'signing_intent': signing_intent,
            'signing_intent_overridden': signing_intent_overridden,
        }

        self.log.debug('plugin result: %s', result)
        return result

    @property
    def odcs_client(self):
        if not self._odcs_client:
            self._odcs_client = get_odcs_session(self.workflow.conf)

        return self._odcs_client

    @property
    def koji_session(self):
        if not self._koji_session:
            self._koji_session = get_koji_session(self.workflow.conf)
        return self._koji_session


class ComposeConfig(object):

    def __init__(self, data, pulp_data, odcs_config, koji_tag=None, arches=None):
        data = data or {}
        self.use_packages = 'packages' in data
        self.packages = data.get('packages', [])
        self.modules = data.get('modules', [])
        self.pulp = {}
        self.arches = arches or []
        self.multilib_arches = []
        self.multilib_method = None
        self.modular_tags = data.get('modular_koji_tags')
        self.module_resolve_tags = data.get('module_resolve_tags')
        self.koji_tag = koji_tag

        if self.modular_tags is True:
            if not self.koji_tag:
                raise ValueError('koji_tag is required when modular_koji_tags is True')
            self.modular_tags = [self.koji_tag]

        if self.module_resolve_tags is True:
            if not self.koji_tag:
                raise ValueError('koji_tag is required when module_resolve_tags is True')
            self.module_resolve_tags = [self.koji_tag]

        if data.get('pulp_repos'):
            for arch in pulp_data or {}:
                if arch in self.arches:
                    self.pulp[arch] = pulp_data[arch]
            self.flags = []
            if data.get(UNPUBLISHED_REPOS):
                self.flags.append(UNPUBLISHED_REPOS)
            if data.get(IGNORE_ABSENT_REPOS):
                self.flags.append(IGNORE_ABSENT_REPOS)

            build_only_content_sets = data.get('build_only_content_sets', {})
            if build_only_content_sets:
                for arch, cont_sets in build_only_content_sets.items():
                    self.pulp[arch] = set(cont_sets).union(self.pulp.get(arch, []))

        for arch in data.get('multilib_arches', []):
            if arch in arches:
                self.multilib_arches.append(arch)
        if self.multilib_arches:
            self.multilib_method = data.get('multilib_method')

        self.odcs_config = odcs_config

        signing_intent_name = data.get('signing_intent', self.odcs_config.default_signing_intent)
        self.set_signing_intent(signing_intent_name)
        self._original_signing_intent_name = signing_intent_name

    def set_signing_intent(self, name):
        self.signing_intent = self.odcs_config.get_signing_intent_by_name(name)

    def has_signing_intent_changed(self):
        return self.signing_intent['name'] != self._original_signing_intent_name

    def render_requests(self):
        self.validate_for_request()

        requests = []
        if self.use_packages:
            requests.append(self.render_packages_request())
        if self.modules:
            requests.append(self.render_modules_request())
        if self.modular_tags:
            requests.append(self.render_modular_tags_request())

        for arch in self.pulp:
            requests.append(self.render_pulp_request(arch))

        return requests

    def render_packages_request(self):
        request = {
            'source_type': 'tag',
            'source': self.koji_tag,
            'packages': self.packages,
            'sigkeys': self.signing_intent['keys'],
        }
        if self.arches:
            request['arches'] = self.arches
        if self.multilib_arches:
            request['multilib_arches'] = self.multilib_arches
            request['multilib_method'] = self.multilib_method
        return request

    def render_modular_tags_request(self):
        request = {
            'source_type': 'tag',
            'source': self.koji_tag,
            'sigkeys': self.signing_intent['keys'],
            'modular_koji_tags': self.modular_tags
        }
        if self.arches:
            request['arches'] = self.arches
        if self.multilib_arches:
            request['multilib_arches'] = self.multilib_arches
            request['multilib_method'] = self.multilib_method
        return request

    def render_modules_request(self):
        # In the Flatpak case, the profile is used to determine which packages
        # are installed into the Flatpak. But ODCS doesn't understand profiles,
        # and they won't affect the compose in any case.
        noprofile_modules = [ModuleSpec.from_str(m).to_str(include_profile=False)
                             for m in self.modules]
        request = {
            'source_type': 'module',
            'source': ' '.join(noprofile_modules),
            'sigkeys': self.signing_intent['keys'],
        }
        if self.module_resolve_tags:
            # For ODCS, modular_koji_tags has a different meaning for source_type=module
            # and for other source types. We use different keys for the two types.
            request['modular_koji_tags'] = self.module_resolve_tags
        if self.arches:
            request['arches'] = self.arches
        return request

    def render_pulp_request(self, arch):
        request = {
            'source_type': 'pulp',
            'source': ' '.join(self.pulp.get(arch, [])),
            'sigkeys': [],
            'flags': self.flags,
            'arches': [arch]
        }
        if arch in self.multilib_arches:
            request['multilib_arches'] = [arch]
            request['multilib_method'] = self.multilib_method
        return request

    def validate_for_request(self):
        """Verify enough information is available for requesting compose."""
        if not self.use_packages and not self.modules and not self.pulp and not self.modular_tags:
            raise ValueError("Nothing to compose (no packages, modules, modular_tags "
                             "or enabled pulp repos)")

        if self.packages and not self.koji_tag:
            raise ValueError('koji_tag is required when packages are used')


class SkipResolveComposesPlugin(Exception):
    pass
