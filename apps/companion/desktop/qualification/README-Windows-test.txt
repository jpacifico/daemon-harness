Daemons: isolated Windows host-attachment test

1. Extract the complete ZIP into a short local path, for example C:\Users\YourName\Downloads\DaemonsTest.
2. Run Run-Windows-Test.cmd as your normal Windows user, not as administrator.
3. Leave the test window open. The app downloads private Python and the pinned
   OpenClaw version, creates synthetic memory and tests activation automatically.
4. A results folder opens when finished. Share summary.json, packaged-result.json
   and fixture-result.json. If one is missing, share the launcher logs instead.

The kit does not replace the installed Companion app. It requires internet for
program downloads, but no account/API key/model charge/Telegram bot. Native tasks
have new random namespaces. Other installations, services and memories are not
modified. Only test-owned bridge/Gateway tasks and a foreground memory worker run.
Normal app launches still have Windows host attachment disabled.

This checks a prepared OpenClaw target attached to a synthetic Hermes Agent
source, with a shared vault. It does NOT install a real Hermes source, test model
quality, perform Telegram delivery or upgrade an existing personal daemon.

After successful cleanup, the test directory reported by summary.json can be
removed to recover download space. If cleanup failed or the app was forcibly
closed, keep those files and report the failure: do not manually delete files
while the test services might still reference them. No automatic upload occurs.
The package is unsigned, like the current research preview app; managed Windows security
policy may block it. Do not disable security software to run this test.
