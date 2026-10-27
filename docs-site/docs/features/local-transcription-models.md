---
sidebar_position: 2
---

# Local Transcription Models

A super admin can download and switch the local Whisper model from **Settings → Transcription → Provider & Model → Local models**, without stopping the stack. The same list of models is used by the Settings screen, by the server's checks, and by `scripts/download-models.py` for first-time setup, so they cannot disagree.

## The model list

Thirteen faster-whisper (CTranslate2) models are offered, from `tiny` to `large-v3` and the turbo and CrisperWhisper builds. Each row shows the download size, an estimated GPU memory need, whether the model is multilingual or English-only, whether it can translate to English, and its status (**In use**, **Downloaded**, **Downloading**, **Not downloaded** and so on). The memory figures are estimates carried over from earlier documentation, not measurements on your hardware.

Only models on this list can be downloaded or selected. Typing a Hugging Face repository name is refused. A deployment that already runs another model keeps running it, but the switch buttons only accept listed models.

## Download a model

1. Open **Settings → Transcription → Provider & Model**, find **Local models**, as a super admin. Other users see the section locked.
2. Choose **Download** on a model. The panel shows free disk space and checks it before starting.
3. Watch the progress bar. You can leave the page; progress also arrives as a live update to the admin who started the download.
4. **Cancel download** stops the download and removes only that model's unfinished files; a partly downloaded model offers **Resume download**.

Only one download runs at a time across the whole deployment. A download interrupted by a restart is reported as failed, and you can start it again. All listed models are public, so no Hugging Face token is needed. If you have saved one under **Speaker Engine**, it is used.

Errors are shown as fixed messages: authentication failed, repository not found, Hugging Face unreachable, disk full, cache not writable, downloads disabled (offline deployment), timed out, or failed.

## Switch the active model

A model must be fully downloaded before it can be made active.

1. Choose **Set active** on a downloaded model and confirm. The dialog says the transcription worker restarts after running jobs finish.
2. The server stores the choice, then asks only the Whisper workers to restart. Jobs already running finish on the old model; new jobs wait for the restarted worker.
3. The panel shows the switch as complete once every restarted worker reports the new model. If no Whisper worker answers, nothing is restarted and you are told so.

The stack itself stays up. Only the transcription worker restarts.

## Cloud transcription providers

Below the model list, **Cloud providers for users** controls which cloud transcription providers plain users may pick and whether they may add their own keys. Shared credentials are the administrator's ASR configurations; the panel shows only a "key set" indicator, never a key. Admins and the server's own default provider are never blocked.

## First-time setup from the command line

`scripts/download-models.py` (called by `scripts/download-models.sh`) remains the way to pre-populate the cache before the first start, for example on an offline host. It uses the same downloader and the same model list as the Settings screen, and refuses a model that is not on the list.
