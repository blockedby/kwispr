#include "config/KwisprSettings.h"

#include "config/EnvFile.h"

#include <QDir>
#include <QFileInfo>
#include <QHostAddress>
#include <QRegularExpression>
#include <QStandardPaths>

#include <cmath>

namespace {
constexpr const char *LocalTranscriptionsUrl = "http://127.0.0.1:19650/v1/audio/transcriptions";
constexpr const char *LegacyLocalTranscriptionsUrl = "http://127.0.0.1:9000/v1/audio/transcriptions";
constexpr const char *OpenAiTranscriptionsUrl = "https://api.openai.com/v1/audio/transcriptions";
constexpr const char *OpenRouterChatUrl = "https://openrouter.ai/api/v1/chat/completions";

void addError(QStringList *errors, const QString &message)
{
    if (errors) {
        errors->append(message);
    }
}

bool isWildcardAddress(const QHostAddress &address)
{
    return address == QHostAddress::Any
        || address == QHostAddress::AnyIPv4
        || address == QHostAddress::AnyIPv6;
}

bool envEnabled(const QString &value, bool fallback)
{
    if (value.trimmed().isEmpty()) {
        return fallback;
    }
    const QString normalized = value.trimmed().toLower();
    return normalized != QLatin1String("0") && normalized != QLatin1String("false") && normalized != QLatin1String("no");
}

QString canonicalOrLegacyValue(const EnvFile &env,
                               const QString &canonicalKey,
                               const QString &legacyKey,
                               const QString &fallback)
{
    if (env.contains(canonicalKey)) {
        return env.value(canonicalKey);
    }
    return env.value(legacyKey, fallback);
}
}

void KwisprSettings::applyLocalPreset(const QString &localModel, const QString &localModelDir, const QString &lang)
{
    backend = "openai-transcriptions";
    apiUrl = LocalTranscriptionsUrl;
    localSttConfigured = true;
    apiKey.clear();
    model = localModel;
    language = lang;
    modelDir = localModelDir;
}

void KwisprSettings::applyOpenAiPreset(const QString &key, const QString &openAiModel, const QString &lang)
{
    backend = "openai-transcriptions";
    apiUrl = OpenAiTranscriptionsUrl;
    localSttConfigured = false;
    apiKey = key;
    model = openAiModel;
    language = lang;
}

void KwisprSettings::applyOpenRouterPreset(const QString &key, const QString &openRouterModel, const QString &prompt, const QString &format)
{
    backend = "openrouter-chat";
    apiUrl = OpenRouterChatUrl;
    localSttConfigured = false;
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
    bool ok = false;
    settings.backend = env.value(QStringLiteral("KWISPR_BACKEND"), settings.backend);
    settings.apiUrl = env.value(QStringLiteral("KWISPR_API_URL"), settings.apiUrl);
    settings.localSttHost = env.value(QStringLiteral("KWISPR_LOCAL_STT_HOST"), settings.localSttHost);
    if (env.contains(QStringLiteral("KWISPR_LOCAL_STT_PORT"))) {
        const QString rawPort = env.value(QStringLiteral("KWISPR_LOCAL_STT_PORT"));
        static const QRegularExpression decimalPort(QStringLiteral("^[0-9]+$"));
        settings.localSttPort = rawPort.toInt(&ok);
        settings.localSttPortValid = decimalPort.match(rawPort).hasMatch()
            && ok && settings.localSttPort >= 1 && settings.localSttPort <= 65535;
        if (!ok) {
            settings.localSttPort = 0;
        }
    } else {
        settings.localSttPort = settings.apiUrl == QLatin1String(LegacyLocalTranscriptionsUrl) ? 9000 : 19650;
    }
    QHostAddress bindAddress;
    settings.localSttAllowLan = bindAddress.setAddress(settings.localSttHost)
        && !bindAddress.isLoopback();
    settings.localSttConfigured = envEnabled(env.value(QStringLiteral("KWISPR_LOCAL_STT_CONFIGURED")), false);
    // The old UI generated this exact URL. Keep both sides on port 9000 until
    // the user changes them; arbitrary custom URLs and ports remain untouched.
    if (settings.apiUrl == QLatin1String(LegacyLocalTranscriptionsUrl)) {
        if (!env.contains(QStringLiteral("KWISPR_LOCAL_STT_HOST"))) {
            settings.localSttHost = QStringLiteral("127.0.0.1");
        }
        if (!env.contains(QStringLiteral("KWISPR_LOCAL_STT_PORT"))) {
            settings.localSttPort = 9000;
        }
        settings.localSttConfigured = true;
    }
    if (!settings.localSttConfigured && settings.apiUrl == QLatin1String(LocalTranscriptionsUrl)) {
        settings.localSttConfigured = true;
    }
    settings.apiKey = env.value(QStringLiteral("KWISPR_API_KEY"), settings.apiKey);
    settings.model = env.value(QStringLiteral("KWISPR_MODEL"), settings.model);
    settings.language = env.value(QStringLiteral("KWISPR_LANGUAGE"), settings.language);
    settings.modelDir = env.value(QStringLiteral("KWISPR_MODEL_DIR"), settings.modelDir);
    settings.audioFormat = env.value(QStringLiteral("KWISPR_AUDIO_FORMAT"), settings.audioFormat);
    settings.transcriptionPrompt = env.value(QStringLiteral("KWISPR_TRANSCRIPTION_PROMPT"), settings.transcriptionPrompt);
    settings.openRouterReferer = canonicalOrLegacyValue(env,
                                                        QStringLiteral("KWISPR_HTTP_REFERER"),
                                                        QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"),
                                                        settings.openRouterReferer);
    settings.openRouterAppTitle = canonicalOrLegacyValue(env,
                                                          QStringLiteral("KWISPR_APP_TITLE"),
                                                          QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"),
                                                          settings.openRouterAppTitle);
    settings.autopaste = envEnabled(env.value(QStringLiteral("KWISPR_AUTOPASTE")), settings.autopaste);
    settings.pasteHotkey = env.value(QStringLiteral("KWISPR_PASTE_HOTKEY"), settings.pasteHotkey);
    const double delay = env.value(QStringLiteral("KWISPR_AUTOPASTE_DELAY")).toDouble(&ok);
    if (ok) {
        settings.autopasteDelay = delay;
    }
    settings.sounds = envEnabled(env.value(QStringLiteral("KWISPR_SOUNDS")), settings.sounds);
    settings.pulseSource = env.value(QStringLiteral("KWISPR_PULSE_SOURCE"), settings.pulseSource);
    const QString vadEnabledKey = env.contains(QStringLiteral("KWISPR_VAD_ENABLED"))
        ? QStringLiteral("KWISPR_VAD_ENABLED")
        : QStringLiteral("KWISPR_VAD");
    settings.vadEnabled = envEnabled(env.value(vadEnabledKey), settings.vadEnabled);
    settings.vadProvider = env.value(QStringLiteral("KWISPR_VAD_PROVIDER"), settings.vadProvider);
    settings.vadModelPath = env.value(QStringLiteral("KWISPR_VAD_MODEL"), settings.vadModelPath);
    const double threshold = env.value(QStringLiteral("KWISPR_VAD_THRESHOLD")).toDouble(&ok);
    if (ok) {
        settings.vadThreshold = threshold;
    }
    const int frameMs = env.value(QStringLiteral("KWISPR_VAD_FRAME_MS")).toInt(&ok);
    if (ok) {
        settings.vadFrameMs = frameMs;
    }
    settings.modelDir = settings.resolvedModelDir();
    return settings;
}

QString KwisprSettings::resolvedModelDir() const
{
    QString path = modelDir.trimmed();
    if (path.isEmpty()) {
        path = QDir(QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation))
                   .filePath(QStringLiteral("kwispr/models"));
    } else if (path == QLatin1String("~")) {
        path = QDir::homePath();
    } else if (path.startsWith(QLatin1String("~/"))) {
        path = QDir::home().filePath(path.mid(2));
    }

    return QDir::cleanPath(QFileInfo(path).absoluteFilePath());
}

void KwisprSettings::writeTo(EnvFile &env) const
{
    env.setValue("KWISPR_BACKEND", backend);
    env.setValue("KWISPR_API_URL", apiUrl);
    env.setValue("KWISPR_LOCAL_STT_HOST", localSttHost);
    env.setValue("KWISPR_LOCAL_STT_PORT", QString::number(localSttPort));
    env.setValue("KWISPR_LOCAL_STT_CONFIGURED", localSttConfigured ? "1" : "0");
    env.setValue("KWISPR_API_KEY", apiKey);
    env.setValue("KWISPR_MODEL", model);
    env.setValue("KWISPR_LANGUAGE", language);
    env.setValue("KWISPR_PULSE_SOURCE", pulseSource);
    env.setValue("KWISPR_AUDIO_FORMAT", audioFormat);
    env.setValue("KWISPR_AUTOPASTE", autopaste ? "1" : "0");
    env.setValue("KWISPR_PASTE_HOTKEY", pasteHotkey);
    env.setValue("KWISPR_AUTOPASTE_DELAY", QString::number(autopasteDelay, 'f', 2));
    env.setValue("KWISPR_SOUNDS", sounds ? "1" : "0");

    env.setValue("KWISPR_MODEL_DIR", resolvedModelDir());
    if (!transcriptionPrompt.isEmpty()) {
        env.setValue("KWISPR_TRANSCRIPTION_PROMPT", transcriptionPrompt);
    }
    env.setValue("KWISPR_HTTP_REFERER", openRouterReferer);
    env.setValue("KWISPR_OPENROUTER_HTTP_REFERER", openRouterReferer);
    env.setValue("KWISPR_APP_TITLE", openRouterAppTitle);
    env.setValue("KWISPR_OPENROUTER_APP_TITLE", openRouterAppTitle);

    const QString vadEnabledValue = vadEnabled ? QStringLiteral("1") : QStringLiteral("0");
    env.setValue("KWISPR_VAD_ENABLED", vadEnabledValue);
    env.setValue("KWISPR_VAD", vadEnabledValue);
    env.setValue("KWISPR_VAD_PROVIDER", vadProvider);
    env.setValue("KWISPR_VAD_MODEL", vadModelPath);
    env.setValue("KWISPR_VAD_THRESHOLD", QString::number(vadThreshold, 'g', 12));
    env.setValue("KWISPR_VAD_FRAME_MS", QString::number(vadFrameMs));
}

QUrl KwisprSettings::localSttHealthUrl() const
{
    if (localSttConfigured) {
        QUrl endpoint(apiUrl);
        QHostAddress destinationAddress;
        if (destinationAddress.setAddress(endpoint.host()) && isWildcardAddress(destinationAddress)) {
            endpoint.setHost(QStringLiteral("127.0.0.1"));
        }
        endpoint.setPath(QStringLiteral("/health"));
        endpoint.setQuery(QString());
        endpoint.setFragment(QString());
        return endpoint;
    }

    QString healthHost = localSttHost.trimmed();
    QHostAddress address;
    if (address.setAddress(healthHost) && isWildcardAddress(address)) {
        healthHost = QStringLiteral("127.0.0.1");
    }
    QUrl endpoint;
    endpoint.setScheme(QStringLiteral("http"));
    endpoint.setHost(healthHost);
    endpoint.setPort(localSttPort);
    endpoint.setPath(QStringLiteral("/health"));
    return endpoint;
}

bool KwisprSettings::validate(QStringList *errors) const
{
    bool ok = true;

    if (apiUrl.startsWith(OpenAiTranscriptionsUrl) && apiKey.trimmed().isEmpty()) {
        ok = false;
        addError(errors, "API key is required for the official OpenAI transcription endpoint.");
    }

    const QUrl endpoint(apiUrl.trimmed());
    const QString endpointScheme = endpoint.scheme().toLower();
    QHostAddress destinationAddress;
    if (!endpoint.isValid() || (endpointScheme != QLatin1String("http") && endpointScheme != QLatin1String("https"))
        || endpoint.host().isEmpty() || endpoint.port() == 0) {
        ok = false;
        addError(errors, "API URL must be a valid HTTP or HTTPS URL with a host and valid port.");
    } else if (destinationAddress.setAddress(endpoint.host()) && isWildcardAddress(destinationAddress)) {
        ok = false;
        addError(errors, "0.0.0.0 and :: are listen addresses, not valid client destinations.");
    }

    QHostAddress bindAddress;
    if (!bindAddress.setAddress(localSttHost.trimmed())) {
        ok = false;
        addError(errors, "Local STT bind address must be a valid IP address.");
    }
    if (!localSttPortValid || localSttPort < 1 || localSttPort > 65535) {
        ok = false;
        addError(errors, "Local STT bind port must be between 1 and 65535.");
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
