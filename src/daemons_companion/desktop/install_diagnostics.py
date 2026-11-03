"""Bounded private installer diagnostics; no credentials, dialogue or host logs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re

from daemons_companion.openclaw.activation import OpenClawReadinessError
from daemons_companion.runtime.bootstrap import HostCommandError
from daemons_companion.setup.transaction import TransactionEngine
from daemon_core.memory.integrity import atomic_write_text

_LABELS = {
    'runtime-layout': 'Copy daemon files',
    'runtime-environment': 'Install Python and dependencies',
    'identity': 'Configure daemon identity',
    'provider-state': 'Configure model access',
    'hermes-profile': 'Connect Hermes plugins',
    'openclaw-profile': 'Connect OpenClaw plugin',
    'openclaw-api-service': 'Register local memory API',
    'openclaw-activation': 'Start and check OpenClaw and memory services',
    'background-services': 'Register private background jobs',
    'hermes-activation': 'Start and check Telegram and memory services',
}


class InstallationProgress:
    """Observe native transaction boundaries without changing their semantics."""
    def __init__(self, adapter, progress, diagnostic=None):
        self.adapter = adapter
        self._observer = diagnostic or (lambda *args: None)
        self.progress = progress
        self.step = ''
        self.phase = ''
        self.failure = None
        self.rollback_failures = []

    def diagnostic(self, *args):
        try:
            self._observer(*args)
        except Exception:
            # Diagnostics must never alter the installation or rollback result.
            pass

    def _call(self, phase, step, *args):
        self.step, self.phase = step.code, phase
        self.adapter.progress = self.progress
        self.progress(f'{_LABELS.get(step.code, step.code)}: {phase}...')
        self.diagnostic(step.code, "started", None, phase)
        try:
            result = getattr(self.adapter, phase)(step, *args)
            self.diagnostic(step.code, "completed", None, phase)
            return result
        except Exception as error:
            self.diagnostic(step.code, "failed", error, phase)
            if phase == 'rollback':
                self.rollback_failures.append((step.code, error))
            elif self.failure is None:
                self.failure = (step.code, phase, error)
            raise

    def snapshot(self, step, directory):
        return self._call('snapshot', step, directory)

    def apply(self, step):
        return self._call('apply', step)

    def verify(self, step):
        return self._call('verify', step)

    def rollback(self, step, directory):
        return self._call('rollback', step, directory)

    def finalize_rollback(self, plan, snapshots):
        finalizer = getattr(self.adapter, 'finalize_rollback', None)
        if callable(finalizer):
            self.progress('Checking the restored private installation...')
            try:
                finalizer(plan, snapshots)
            except Exception as error:
                self.rollback_failures.append(('finalize-rollback', error))
                raise


def _scrub(value, scope):
    text = str(value)
    # Only diagnostics from this installer are accepted below. These filters
    # additionally suppress accidental credentials in dependency-manager errors.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(c for c in text if c in '\n\t' or (c.isprintable() and c != '\x7f'))
    text = re.sub(r'(?im)^.*(?:authorization\s*[:=]|["\x27]?(?:[a-z_]*token|password|secret|api_key)["\x27]?\s*[:=]).*$', '[credential detail removed]', text)
    text = re.sub(r'\b[0-9]{5,20}:[A-Za-z0-9_-]{20,}\b', '[token removed]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', '[authorization removed]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[key removed]', text)
    text = re.sub(r'https?://[^\s<>"\x27]+', '[URL removed]', text)
    text = re.sub(r'\b[A-Za-z0-9_-]{40,}(?:\.[A-Za-z0-9_-]+)*\b', '[opaque value removed]', text)
    for path in sorted((str(scope.home), str(scope.attempt)), key=len, reverse=True):
        text = text.replace(path, '<private installation>')
    return text.strip()[:4000]


def _causes(error, scope):
    causes = []
    seen = set()
    while error is not None and id(error) not in seen and len(causes) < 6:
        seen.add(id(error))
        item = {'type': type(error).__name__[:80], 'message': _scrub(error, scope)}
        for key in ('errno', 'winerror', 'exit_code'):
            value = getattr(error, key, None)
            if type(value) is int:
                item[key] = value
        if isinstance(error, (HostCommandError, OpenClawReadinessError)):
            # Fixed installation commands and readiness probes only (uv, Python,
            # scheduler, plugins, gateway health); never OAuth or chat logs.
            item['command_detail'] = _scrub(error.diagnostic, scope)
        causes.append(item)
        error = error.__cause__
    return causes


def _revision():
    value = os.environ.get('DAEMON_HARNESS_COMPANION_REVISION', '')
    return value if re.fullmatch(r'[a-f0-9]{40}', value) else None


def report_installation_failure(ui, scope, observed, error, transaction_id):
    """Report after rollback; failure to save diagnostics must not mask the cause."""
    step, phase, cause = observed.failure or (observed.step, observed.phase, error)
    causes = _causes(cause, scope)
    try:
        state = TransactionEngine(scope.home / 'transactions').inspect(transaction_id)
        status = state.status
        failed_steps = list(state.rollback_failed_steps)
    except (OSError, ValueError, RuntimeError):
        status, failed_steps = 'unknown', []
    data = {
        'schema': 'daemon.installation-diagnostic.v1',
        'at': datetime.now(timezone.utc).isoformat(),
        # The app build that ran, so a report names its own version.
        'revision': _revision(),
        'transaction_id': transaction_id,
        'step': step, 'phase': phase, 'causes': causes,
        'transaction_status': status, 'rollback_failed_steps': failed_steps,
        'rollback_causes': [{'step': code, 'causes': _causes(failure, scope)}
                            for code, failure in observed.rollback_failures[:8]],
    }
    ui.say('Failed step: ' + _LABELS.get(step, step or 'Prepare installation') + ' (' + phase + ').')
    for item in causes:
        ui.say('Installation cause: ' + item['type'] + ': ' + item['message'])
        if item.get('command_detail'):
            ui.say('Command details: ' + item['command_detail'])
    if status == 'rolled_back':
        ui.say('Rollback completed for the changes made by this installation attempt.')
    elif status == 'rollback_failed':
        ui.say('Rollback needs recovery. Do not reinstall into this profile. Affected steps: ' + ', '.join(failed_steps))
    else:
        ui.say('Rollback status could not be confirmed. Keep this installation folder for diagnosis.')
    path = scope.attempt / 'installation-diagnostic.json'
    try:
        scope.validate_home(scope.home)
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('Diagnostic path must be a direct private file.')
        atomic_write_text(path, json.dumps(data, ensure_ascii=True, indent=2) + '\n')
        path.chmod(0o600)
    except (OSError, ValueError, RuntimeError):
        ui.say('The diagnostic file could not be saved. Copy the failed step and installation cause shown above.')
    else:
        ui.say('Diagnostic saved: ' + str(path))
        ui.say('For help, share this diagnostic file, never your bot token or .env file.')
