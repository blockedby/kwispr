# Transcription reliability verification — 2026-09-22

## Installed recording build

Commit `5945f8f` was installed and pushed to `main`. It includes the dictation
text-preservation fix, independent meeting languages, optional response language
metadata, and the file-manager action. The installed runtime executable and all
18 native libraries matched the verified release artifacts. The running process
matched the installed executable; the local health check passed.

A saved 71.55-second dictation reproduced the original loss: the legacy subtitle
cleanup removed legitimate speech starting at the word “субтитры”. The installed
CLI retained the complete 643-character result, including the final sentence,
using an isolated clipboard helper. Original audio hashes were unchanged.

A bilingual punctuation example preserved VPN/KDE/TUI in a real Russian clip and
restored English in a clip that the Russian-only example distorted. The personal
vocabulary was left unchanged. Recovered dictation and meeting outputs were saved
separately from the originals.

Live installation finished before the user's next recording. During that recording,
read-only checks observed both WAV files growing by 262,144 bytes over ten seconds.
No subsequent application/service restart or configuration change was performed.
File growth confirms ongoing writes; it does not by itself verify signal content.

## Prepared follow-up build

The optional RU/EN candidate restriction and its settings UI remain separate from
the installed recording build. Actual API and full-recording evidence is recorded
in [meeting-language-verification.md](meeting-language-verification.md).

Final validation: Python discovery passed 107 tests with two optional audio tests
skipped; those two disposable PulseAudio capture/coexistence checks also passed
when explicitly enabled. The final KDE Podman suite passed all 14 suites. Rust
Podman tests passed 33 runtime and five wrapper tests; the model-free native
language selector regression also passed. Real-font settings renders were checked
at 520×560 and 760×720, and meeting renders at 400×460 and 600×660.

The KDE run exposed an existing dialog teardown use-after-free: a child QProcess
could deliver its finished callback after state strings had been destroyed. ASan
reproduced it before the fix; disconnecting child callbacks before member teardown
passed the full 20-case MeetingDialog ASan run and the final normal suite. Ordinary
window close hides the retained dialog; destruction occurs during application
shutdown. This fix is part of the prepared follow-up build.
