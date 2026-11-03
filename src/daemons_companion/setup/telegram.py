"""Telegram instructions and local navigation before any secret or mutation."""
from __future__ import annotations

import re

from daemons_companion.desktop.terminal import Back, Quit
from daemons_companion.backup.restore import pending_restore


def create_bot(ui):
    """BotFather guide shared by a first installation and an added host."""
    while True:
        ui.say('1. Search for @BotFather in Telegram Web. Open the verified account and send /newbot.')
        ui.say('2. Choose a display name, then an available username ending in bot. Create a new bot dedicated to this installation.')
        ui.say('3. Keep BotFather’s reply open. You will need its token (digits, a colon, then letters) shortly — not the t.me link.')
        choice = ui.choose('Create your Telegram bot', ('My bot is created — continue', 'Open Telegram Web again', 'Set up later and close'))
        if choice == 0:
            return
        if choice == 2:
            raise Quit
        ui.open_link('https://web.telegram.org/')


def prepare_telegram(ui, destination, *, host="hermes"):
    step = 0
    user_id = ''
    while True:
        try:
            if step == 0:
                ui.say('Open Telegram Web on this computer and sign in to your usual account.')
                ui.say('To scan the QR code: on your phone, open Telegram → Settings → Devices → Link Desktop Device. You can also sign in with your phone number.')
                ui.say('Keep the browser tab open, then choose “I am signed in” below.')
                choice = ui.choose('Connect Telegram Web', ('Open Telegram Web', 'I am signed in — continue', 'Set up later and close'))
                if choice == 0:
                    ui.open_link('https://web.telegram.org/')
                    continue
                if choice == 2:
                    raise Quit
                step = 1
            elif step == 1:
                restored = pending_restore()
                if restored is not None:
                    user_id = user_id or restored.owner
                    ui.say(f'This memory belongs to Telegram account {restored.owner}. Sign in to that account in Telegram Web.')
                ui.say('Your Telegram account number lets the daemon accept messages only from you. It is not your phone number, @username or bot token.')
                ui.say('In the same Telegram Web account, search for exactly @userinfobot and send /start yourself. Do not forward someone else’s message.')
                ui.say('Copy only the digits on the reply’s “Id” line. If there is no reply, check the username and send /start again. Use Back to return.')
                user_id = ui.field('Paste the digits from your own Id line', user_id, maximum=20)
                if re.fullmatch(r'[1-9][0-9]{0,19}', user_id) is None:
                    ui.say('Enter the numeric ID given by @userinfobot, not a username or your bot token.')
                    user_id = ''
                    continue
                if restored is not None and user_id != restored.owner:
                    ui.say(f'This backup belongs to Telegram account {restored.owner}. Enter that account’s ID, or install a new daemon instead.')
                    user_id = restored.owner
                    continue
                step = 2
            elif step == 2:
                create_bot(ui)
                step = 3
            else:
                ui.say(f'Only Telegram account {user_id} will be allowed. Destination: {destination}')
                ui.say('Canonical voice templates are copied into the new memory. Existing memory files are never overwritten.')
                ui.say('This installs the daemon and starts five private jobs: Telegram gateway, local memory API, local memory search, memory worker and nightly maintenance.' if host == 'openclaw' else 'This installs the daemon and starts four private jobs: Telegram gateway, local memory search, memory worker and nightly maintenance.')
                ui.say('Model usage applies to replies and memory work. Keep this computer awake for Telegram replies.')
                choice = ui.choose('Install and start this private daemon?', ('Install and start', 'Back to Telegram details', 'Quit'))
                if choice == 1:
                    step = 2
                elif choice == 2:
                    raise Quit
                else:
                    return user_id
        except Back:
            if step == 0:
                # Returning to the parent preserves its reviewed model/identity.
                raise
            step -= 1


class TelegramSetupError(RuntimeError):
    """Fixed, secret-free explanation of a native Telegram setup failure."""


def verify_telegram_token(ui, configure):
    """Retry only the hidden token, preserving the reviewed model and account ID."""
    ui.say('In BotFather’s reply, copy the full token (digits, a colon, then letters), not the bot link. Paste it in the hidden field below.')
    while True:
        token = ui.secret('Paste your BotFather token (hidden): ').strip()
        try:
            if token == ':quit':
                raise Quit
            with ui.activity('Verifying your Telegram bot...'):
                identity = configure(token)
        except TelegramSetupError as error:
            ui.say(str(error))
        else:
            ui.say('Token accepted — Telegram verified @' + identity['username'] + '. Installing your daemon next.')
            return identity
        finally:
            del token
        while True:
            try:
                choice = ui.choose('Telegram verification did not finish',
                                   ('Enter the token again', 'Open Telegram Web', 'Quit'))
            except Back:
                break
            if choice == 0:
                break
            if choice == 2:
                raise Quit
            ui.open_link('https://web.telegram.org/')
