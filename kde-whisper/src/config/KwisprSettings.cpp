#include "config/KwisprSettings.h"

#include "config/EnvFile.h"

#include <cmath>

#include <QMap>

namespace {
constexpr const char *LocalTranscriptionsUrl = "http://127.0.0.1:9000/v1/audio/transcriptions";
constexpr const char *OpenAiTranscriptionsUrl = "https://api.openai.com/v1/audio/transcriptions";
constexpr const char *OpenRouterChatUrl = "https://openrouter.ai/api/v1/chat/completions";

void addError(QStringList *errors, const QString &message)
{
    if (errors) {
        errors->append(message);
    }
}
}

void KwisprSettings::applyLocalPreset(const QString &localModel, const QString &localModelDir, const QString &lang)
{
    backend = "openai-transcriptions";
    apiUrl = LocalTranscriptionsUrl;
    apiKey.clear();
    model = localModel;
    language = lang;
    modelDir = localModelDir;
}

void KwisprSettings::applyOpenAiPreset(const QString &key, const QString &openAiModel, const QString &lang)
{
    backend = "openai-transcriptions";
    apiUrl = OpenAiTranscriptionsUrl;
    apiKey = key;
    model = openAiModel;
    language = lang;
}

void KwisprSettings::applyOpenRouterPreset(const QString &key, const QString &openRouterModel, const QString &prompt, const QString &format)
{
    backend = "openrouter-chat";
    apiUrl = OpenRouterChatUrl;
    apiKey = key;
    model = openRouterModel;
    transcriptionPrompt = prompt;
    audioFormat = format;
    openRouterReferer = "https://github.com/blockedby/kwispr";
    openRouterAppTitle = "KDE Whisper";
}

KwisprSettings KwisprSettings::fromEnv(const EnvFile &env)
{
    KwisprSettings settings;
    const QMap<QString, QString> defaults = EnvFile::defaults();
    auto value = [&env, &defaults](const QString &key, const QString &fallback = QString()) {
        return env.value(key, defaults.value(key, fallback));
    };
    auto boolValue = [&value](const QString &key, bool fallback) {
        const QString raw = value(key, fallback ? QStringLiteral("1") : QStringLiteral("0")).trimmed().toLower();
        if (raw == QLatin1String("1") || raw == QLatin1String("true") || raw == QLatin1String("yes")) {
            return true;
        }
        if (raw == QLatin1String("0") || raw == QLatin1String("false") || raw == QLatin1String("no")) {
            return false;
        }
        return fallback;
    };

    settings.backend = value(QStringLiteral("KWISPR_BACKEND"), settings.backend);
    settings.apiUrl = value(QStringLiteral("KWISPR_API_URL"), settings.apiUrl);
    settings.apiKey = value(QStringLiteral("KWISPR_API_KEY"), settings.apiKey);
    settings.model = value(QStringLiteral("KWISPR_MODEL"), settings.model);
    settings.language = value(QStringLiteral("KWISPR_LANGUAGE"), settings.language);
    settings.modelDir = value(QStringLiteral("KWISPR_MODEL_DIR"), settings.modelDir);
    settings.audioFormat = value(QStringLiteral("KWISPR_AUDIO_FORMAT"), settings.audioFormat);
    settings.transcriptionPrompt = value(QStringLiteral("KWISPR_TRANSCRIPTION_PROMPT"), settings.transcriptionPrompt);
    settings.openRouterReferer = value(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"), settings.openRouterReferer);
    settings.openRouterAppTitle = value(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"), settings.openRouterAppTitle);
    settings.autopaste = boolValue(QStringLiteral("KWISPR_AUTOPASTE"), settings.autopaste);
    settings.pasteHotkey = value(QStringLiteral("KWISPR_PASTE_HOTKEY"), settings.pasteHotkey);
    bool ok = false;
    const double delay = value(QStringLiteral("KWISPR_AUTOPASTE_DELAY"), QString::number(settings.autopasteDelay)).toDouble(&ok);
    if (ok) {
        settings.autopasteDelay = delay;
    }
    settings.sounds = boolValue(QStringLiteral("KWISPR_SOUNDS"), settings.sounds);
    settings.pulseSource = value(QStringLiteral("KWISPR_PULSE_SOURCE"), settings.pulseSource);
    settings.vadEnabled = boolValue(QStringLiteral("KWISPR_VAD"), settings.vadEnabled);
    settings.vadProvider = value(QStringLiteral("KWISPR_VAD_PROVIDER"), settings.vadProvider);
    settings.vadModelPath = value(QStringLiteral("KWISPR_VAD_MODEL"), settings.vadModelPath);
    const double threshold = value(QStringLiteral("KWISPR_VAD_THRESHOLD"), QString::number(settings.vadThreshold)).toDouble(&ok);
    if (ok) {
        settings.vadThreshold = threshold;
    }
    const int frameMs = value(QStringLiteral("KWISPR_VAD_FRAME_MS"), QString::number(settings.vadFrameMs)).toInt(&ok);
    if (ok) {
        settings.vadFrameMs = frameMs;
    }
    return settings;
}

void KwisprSettings::writeTo(EnvFile &env) const
{
    env.setValue("KWISPR_BACKEND", backend);
    env.setValue("KWISPR_API_URL", apiUrl);
    env.setValue("KWISPR_API_KEY", apiKey);
    env.setValue("KWISPR_MODEL", model);
    env.setValue("KWISPR_LANGUAGE", language);
    env.setValue("KWISPR_PULSE_SOURCE", pulseSource);
    env.setValue("KWISPR_AUDIO_FORMAT", audioFormat);
    env.setValue("KWISPR_AUTOPASTE", autopaste ? "1" : "0");
    env.setValue("KWISPR_PASTE_HOTKEY", pasteHotkey);
    env.setValue("KWISPR_AUTOPASTE_DELAY", QString::number(autopasteDelay, 'f', 2));
    env.setValue("KWISPR_SOUNDS", sounds ? "1" : "0");

    if (!modelDir.isEmpty()) {
        env.setValue("KWISPR_MODEL_DIR", modelDir);
    }
    if (!transcriptionPrompt.isEmpty()) {
        env.setValue("KWISPR_TRANSCRIPTION_PROMPT", transcriptionPrompt);
    }
    if (!openRouterReferer.isEmpty()) {
        env.setValue("KWISPR_OPENROUTER_HTTP_REFERER", openRouterReferer);
    }
    if (!openRouterAppTitle.isEmpty()) {
        env.setValue("KWISPR_OPENROUTER_APP_TITLE", openRouterAppTitle);
    }

    env.setValue("KWISPR_VAD", vadEnabled ? "1" : "0");
    env.setValue("KWISPR_VAD_PROVIDER", vadProvider);
    env.setValue("KWISPR_VAD_MODEL", vadModelPath);
    env.setValue("KWISPR_VAD_THRESHOLD", QString::number(vadThreshold, 'g', 12));
    env.setValue("KWISPR_VAD_FRAME_MS", QString::number(vadFrameMs));
}

bool KwisprSettings::validate(QStringList *errors) const
{
    bool ok = true;

    if (apiUrl.startsWith(OpenAiTranscriptionsUrl) && apiKey.trimmed().isEmpty()) {
        ok = false;
        addError(errors, "API key is required for the official OpenAI transcription endpoint.");
    }

    const QString normalizedHotkey = pasteHotkey.trimmed().toLower();
    if (normalizedHotkey != "ctrl-v" && normalizedHotkey != "ctrl-shift-v" && normalizedHotkey != "shift-insert") {
        ok = false;
        addError(errors, "Unsupported paste hotkey. Use ctrl-v, ctrl-shift-v, or shift-insert.");
    }

    if (vadEnabled) {
        if (vadProvider.compare("silero", Qt::CaseInsensitive) == 0 && vadModelPath.trimmed().isEmpty()) {
            ok = false;
            addError(errors, "Silero VAD requires a model path.");
        }
        if (!std::isfinite(vadThreshold) || vadThreshold < 0.0) {
            ok = false;
            addError(errors, "VAD threshold must be finite and non-negative.");
        }
        if (vadFrameMs <= 0) {
            ok = false;
            addError(errors, "VAD frame duration must be greater than zero.");
        }
    }

    return ok;
}
