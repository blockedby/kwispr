#pragma once

#include <QString>
#include <QStringList>
#include <QUrl>

class EnvFile;

class KwisprSettings {
public:
    QString backend = "openai-transcriptions";
    QString apiUrl = "https://api.openai.com/v1/audio/transcriptions";
    QString localSttHost = "127.0.0.1";
    int localSttPort = 19650;
    bool localSttPortValid = true;
    bool localSttAllowLan = false;
    bool localSttConfigured = false;
    QString apiKey;
    QString model = "whisper-1";
    QString language;
    QString whisperAllowedLanguages;
    QString modelDir;
    QString audioFormat = "wav";
    QString transcriptionPrompt;
    QString whisperPrompt;
    QString vocabulary;
    int stopDelayMs = 0;
    bool preserveAudioTail = false;
    QString openRouterReferer = "https://github.com/blockedby/kwispr";
    QString openRouterAppTitle = "KDE Whisper";

    bool autopaste = true;
    QString pasteHotkey = "shift-insert";
    double autopasteDelay = 0.30;

    bool sounds = true;
    QString pulseSource = "default";

    bool vadEnabled = false;
    QString vadProvider = "energy";
    QString vadModelPath;
    double vadThreshold = 0.01;
    int vadFrameMs = 30;

    void applyLocalPreset(const QString &localModel, const QString &localModelDir, const QString &lang);
    void applyOpenAiPreset(const QString &key, const QString &openAiModel, const QString &lang);
    void applyOpenRouterPreset(const QString &key, const QString &openRouterModel, const QString &prompt, const QString &format);

    static KwisprSettings fromEnv(const EnvFile &env);
    static QString normalizedVocabulary(const QString &value);
    static bool validWhisperAllowedLanguages(const QString &value);
    static QString normalizedWhisperAllowedLanguages(const QString &value);
    QString combinedWhisperPrompt() const;
    QString resolvedModelDir() const;
    QUrl localSttHealthUrl() const;
    void writeTo(EnvFile &env) const;
    bool validate(QStringList *errors = nullptr) const;
};
