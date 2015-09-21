"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from email.mime.text import MIMEText
import os
import smtplib
import socket
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from dockerfile_parse import DockerfileParser
import requests

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.source import GitSource


class SendMailPlugin(ExitPlugin):
    """This plugins sends notifications about build results.

    Example configuration (see arguments for init for detailed explanation):
        "exit_plugins": [{
                "name": "sendmail",
                "args": {
                    "send_on": ["auto_canceled", "auto_fail"],
                    "url": "https://openshift-instance.com",
                    "pdc_url": "https://pdc-instance.com",
                    # pdc_secret_path is filled in automatically by osbs-client
                    "pdc_secret_path": "/path/to/file/with/pdc/token",
                    "smtp_url": "smtp-server.com",
                    "from_address": "osbs@mycompany.com",
                    "error_addresses": ["admin@mycompany.com"],
                    # optional arguments follow
                    "pdc_verify_cert": true,
                    "pdc_component_df_label": "BZComponent"
                }
        }]
    """
    key = "sendmail"

    # symbolic constants for states
    MANUAL_SUCCESS = 'manual_success'
    MANUAL_FAIL = 'manual_fail'
    AUTO_SUCCESS = 'auto_success'
    AUTO_FAIL = 'auto_fail'
    AUTO_CANCELED = 'auto_canceled'

    allowed_states = set([MANUAL_SUCCESS, MANUAL_FAIL, AUTO_SUCCESS, AUTO_FAIL, AUTO_CANCELED])

    PDC_TOKEN_FILE = 'pdc.token'

    def __init__(self, tasker, workflow, send_on=None, url=None, pdc_url=None,
                 pdc_verify_cert=True, pdc_component_df_label="BZComponent", pdc_secret_path=None,
                 smtp_url=None, from_address=None, error_addresses=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: URL to OSv3 instance where the build logs are stored
        :param send_on: list of build states when a notification should be sent
        :param pdc_url: URL of PDC to query for contact information
        :param pdc_verify_cert: whether or not to verify SSL cert of PDC (defaults to True)
        :param pdc_component_df_label: name of Dockerfile label to use as PDC global_component
        :param pdc_secret_path: path to pdc.token file; $SOURCE_SECRET_PATH otherwise
        :param smtp_url: URL of SMTP server to use to send the message (e.g. "foo.com:25")
        :param from_address: the "From" of the notification email
        :param error_addresses: list of email addresses where to send an email if there's an error
            (e.g. if we can't find out who to notify about the failed build)
        """
        super(SendMailPlugin, self).__init__(tasker, workflow)
        self.url = url
        self.send_on = send_on
        self.pdc_url = pdc_url
        self.pdc_verify_cert = pdc_verify_cert
        self.pdc_component_df_label = pdc_component_df_label
        self.pdc_secret_path = pdc_secret_path
        self.smtp_url = smtp_url
        self.from_address = from_address
        self.error_addresses = error_addresses

    def _should_send(self, rebuild, success, canceled):
        """Return True if mail should be sent under given conditions and `self.send_on`."""
        should_send = False

        should_send_mapping = {
            self.MANUAL_SUCCESS: not rebuild and success,
            self.MANUAL_FAIL: not rebuild and not success,
            self.AUTO_SUCCESS: rebuild and success,
            self.AUTO_FAIL: rebuild and not success,
            self.AUTO_CANCELED: rebuild and canceled
        }

        for state in self.send_on:
            should_send |= should_send_mapping[state]
        return should_send

    def _render_mail(self, rebuild, success, canceled):
        """Render and return subject and body of the mail to send."""
        subject_template = 'Image %(image)s; Status %(endstate)s; Submitted by %(user)s'
        body_template = '\n'.join([
            'Image: %(image)s',
            'Status: %(endstate)s',
            'Submitted by: %(user)s',
            'Logs: %(logs)s',
        ])

        endstate = None
        if canceled:
            endstate = 'canceled'
        else:
            endstate = 'successful' if success else 'failed'
        url = None
        if self.url and self.workflow.openshift_build_selflink:
            url = urljoin(self.url, self.workflow.openshift_build_selflink + '/log')

        formatting_dict = {
            'image': self.workflow.image,
            'endstate': endstate,
            'user': '<autorebuild>' if rebuild else 'TODO user',
            'logs': url
        }
        return (subject_template % formatting_dict, body_template % formatting_dict)

    def _get_pdc_token(self):
        if self.pdc_secret_path is not None:
            token_file = os.path.join(self.pdc_secret_path, self.PDC_TOKEN_FILE)
        else:
            token_file = os.path.join(os.environ['SOURCE_SECRET_PATH'], self.PDC_TOKEN_FILE)

        self.log.debug('getting PDC token from file %s', token_file)

        with open(token_file, 'r') as f:
            return f.read().strip()

    def _get_component_label(self):
        """Get value of Dockerfile label that is to be used as `global_component` to query
        PDC release-components API endpoint.
        """
        labels = DockerfileParser(self.workflow.builder.df_path).labels
        if self.pdc_component_df_label not in labels:
            raise PluginFailedException('No %s label in Dockerfile, can\'t get PDC component',
                                        self.pdc_component_df_label)
        return labels[self.pdc_component_df_label]

    def _get_receivers_list(self):
        """Return list of receivers of the notification.

        :raises RuntimeError: if PDC can't be contacted or doesn't provide sufficient data
        :raises PluginFailedException: if there's a critical error while getting PDC data
        """

        # TODO: document what this plugin expects to be in Dockerfile/where it gets info from
        global_component = self._get_component_label()
        # this relies on bump_release plugin configuring source.git_commit to actually be
        #  branch name, not a commit
        if not isinstance(self.workflow.source, GitSource):
            raise PluginFailedException('Source is not of type "GitSource", panic!')
        git_branch = self.workflow.source.git_commit
        try:
            # TODO: verify=False should be removed and proper certificates added
            r = requests.get(urljoin(self.pdc_url, 'rest_api/v1/release-components/'),
                             headers={'Authorization': 'Token %s' % self._get_pdc_token()},
                             params={'global_component': global_component},
                             verify=self.pdc_verify_cert)
        except requests.RequestException as e:
            self.log.error('failed to connect to PDC: %s', str(e))
            raise RuntimeError(e)

        if r.status_code != 200:
            self.log.error('PDC returned status code %s, full response: %s' %
                           (r.status_code, r.text))
            raise RuntimeError('PDC returned non-200 status code (%s), see referenced build log' %
                               r.status_code)
        # TODO: open an RFE for PDC to allow querying by dist_git_branch, now we have to filter
        #  the correct result manually
        found = []
        for result in r.json()['results']:
            if result['dist_git_branch'] == git_branch:
                found.append(result)

        if len(found) != 1:
            self.log.error('expected 1 PDC component, found %s: %s' %
                           (len(found), found))
            raise RuntimeError('Expected to find exactly 1 PDC component, found %s, '
                               'see referenced build log' % len(found))

        send_to = []
        contacts = found[0].get('contacts', [])
        for contact in contacts:
            # TODO: find out if Build_Owner is the correct role (make this configurable?)
            # I'm not sure, but it seems that there can be more people with the role
            if contact['contact_role'] == 'Build_Owner':
                send_to.append(contact['email'])

        if len(send_to) == 0:
            self.log.error('no Build_Owner role for the component')
            raise RuntimeError('no Build_Owner role for the component')

        return send_to

    def _send_mail(self, receivers_list, subject, body):
        """Actually sends the mail with `subject` and `body` to all members of `receivers_list`."""
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = self.from_address
        msg['To'] = ', '.join(receivers_list)

        s = None
        try:
            s = smtplib.SMTP(self.smtp_url)
            s.sendmail(self.from_address, receivers_list, msg.as_string())
        except (socket.gaierror, smtplib.SMTPException) as e:
            raise PluginFailedException('Error communicating with SMTP server: %s' % str(e))
        finally:
            if s is not None:
                s.quit()

    def run(self):
        # verify that given states are subset of allowed states
        unknown_states = set(self.send_on) - self.allowed_states
        if len(unknown_states) > 0:
            raise PluginFailedException('Unknown state(s) "%s" for sendmail plugin' %
                                        '", "'.join(sorted(unknown_states)))

        rebuild = is_rebuild(self.workflow)
        success = not self.workflow.build_failed
        canceled = self.workflow.autorebuild_canceled

        self.log.info('checking conditions for sending notification ...')
        if self._should_send(rebuild, success, canceled):
            self.log.info('notification about build result will be sent')
            subject, body = self._render_mail(rebuild, success, canceled)
            try:
                self.log.debug('getting list of receivers for this component ...')
                receivers = self._get_receivers_list()
            except RuntimeError as e:
                self.log.error('couldn\'t get list of receivers, sending error message ...')
                # TODO: maybe improve the error message/subject
                body = '\n'.join([
                    'Failed to get contact for %s, error: %s' % (str(self.workflow.image), str(e)),
                    'Since your address is in "error_addresses", this email was sent to you to '
                    'take action on this.',
                    'Wanted to send following mail:',
                    '',
                    body
                ])
                receivers = self.error_addresses
            self.log.info('sending notification to %s ...' % receivers)
            self._send_mail(receivers, subject, body)
        else:
            self.log.info('conditions for sending notification not met, doing nothing')
