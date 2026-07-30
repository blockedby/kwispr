#include <QtTest/QtTest>

#include "config/EnvFile.h"
#include "config/KwisprSettings.h"

#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>

class SettingsModelTest : public QObject {
    Q_OBJECT

private slots:
    void localPresetWritesLocalSttKeys();
    void localSttConnectionRoundTripsAndHealthUsesClientEndpoint();
    void legacyGeneratedPortStaysMatchedOnUpgrade();
    void localSttValidationRejectsInvalidValues();
    void modelDirResolutionUsesExplicitPathsOrStandardDefault();
    void openAiPresetValidatesApiKeyForOfficialEndpoint();
    void openRouterPresetWritesChatBackendKeys();
    void openRouterMetadataEnvUsesCanonicalKeysWithLegacyCompatibility();
    void pasteHotkeyValidationAllowsOnlySupportedValues();
    void vadDefaultsMatchEnergyRuntime();
    void vadEnvUsesCanonicalKeyWithLegacyCompatibility();
    void vadValidationMatchesRuntimeRequirements();
};

void SettingsModelTest::localPresetWritesLocalSttKeys()
{
    KwisprSettings settings;
    settings.applyLocalPreset("whisper-large-v3-turbo", "/models", "ru");

    EnvFile env;
    settings.writeTo(env);

    QCOMPARE(env.value("KWISPR_BACKEND"), QString("openai-transcriptions"));
    QCOMPARE(env.value("KWISPR_API_URL"), QString("http://127.0.0.1:19650/v1/audio/transcriptions"));
    QCOMPARE(env.value("KWISPR_LOCAL_STT_HOST"), QString("127.0.0.1"));
    QCOMPARE(env.value("KWISPR_LOCAL_STT_PORT"), QString("19650"));
    QCOMPARE(env.value("KWISPR_LOCAL_STT_CONFIGURED"), QString("1"));
    QCOMPARE(env.value("KWISPR_API_KEY"), QString(""));
    QCOMPARE(env.value("KWISPR_MODEL"), QString("whisper-large-v3-turbo"));
    QCOMPARE(env.value("KWISPR_LANGUAGE"), QString("ru"));
    QCOMPARE(env.value("KWISPR_MODEL_DIR"), QString("/models"));
}

void SettingsModelTest::localSttConnectionRoundTripsAndHealthUsesClientEndpoint()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("remote/catalog-slug"), QString(), QString());
    settings.apiUrl = QStringLiteral("http://stt-box.lan:24567/v1/audio/transcriptions");
    settings.localSttHost = QStringLiteral("0.0.0.0");
    settings.localSttPort = 23456;
    settings.localSttAllowLan = true;

    EnvFile env;
    settings.writeTo(env);
    const KwisprSettings loaded = KwisprSettings::fromEnv(env);
    QCOMPARE(loaded.apiUrl, settings.apiUrl);
    QCOMPARE(loaded.localSttHost, QStringLiteral("0.0.0.0"));
    QCOMPARE(loaded.localSttPort, 23456);
    QVERIFY(loaded.localSttAllowLan);
    QVERIFY(loaded.localSttConfigured);
    QCOMPARE(loaded.model, QStringLiteral("remote/catalog-slug"));
    QCOMPARE(loaded.localSttHealthUrl(), QUrl(QStringLiteral("http://stt-box.lan:24567/health")));

    loaded.writeTo(env);
    QCOMPARE(env.value(QStringLiteral("KWISPR_API_URL")), settings.apiUrl);
    QCOMPARE(env.value(QStringLiteral("KWISPR_LOCAL_STT_HOST")), QStringLiteral("0.0.0.0"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_LOCAL_STT_PORT")), QStringLiteral("23456"));
}

void SettingsModelTest::legacyGeneratedPortStaysMatchedOnUpgrade()
{
    EnvFile env;
    env.setValue(QStringLiteral("KWISPR_BACKEND"), QStringLiteral("openai-transcriptions"));
    env.setValue(QStringLiteral("KWISPR_API_URL"), QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));

    const KwisprSettings settings = KwisprSettings::fromEnv(env);
    QCOMPARE(settings.apiUrl, QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));
    QCOMPARE(settings.localSttHost, QStringLiteral("127.0.0.1"));
    QCOMPARE(settings.localSttPort, 9000);
    QVERIFY(settings.localSttConfigured);

    settings.writeTo(env);
    QCOMPARE(env.value(QStringLiteral("KWISPR_API_URL")), QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_LOCAL_STT_PORT")), QStringLiteral("9000"));
}

void SettingsModelTest::localSttValidationRejectsInvalidValues()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("remote-slug"), QString(), QString());
    settings.apiUrl = QStringLiteral("not a URL");
    settings.localSttHost = QStringLiteral("not-an-address");
    settings.localSttPort = 70000;
    QStringList errors;
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains(QStringLiteral("API URL")));
    QVERIFY(errors.join('\n').contains(QStringLiteral("bind address")));
    QVERIFY(errors.join('\n').contains(QStringLiteral("bind port")));

    settings.apiUrl = QStringLiteral("http://0.0.0.0:19650/v1/audio/transcriptions");
    settings.localSttHost = QStringLiteral("0.0.0.0");
    settings.localSttPort = 19650;
    errors.clear();
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains(QStringLiteral("client destinations")));

    settings.apiUrl = QStringLiteral("http://remote-box.lan:19650/v1/audio/transcriptions");
    errors.clear();
    QVERIFY2(settings.validate(&errors), qPrintable(errors.join('\n')));

    EnvFile invalidPortEnv;
    invalidPortEnv.setValue(QStringLiteral("KWISPR_LOCAL_STT_PORT"), QStringLiteral("not-a-port"));
    const KwisprSettings invalidPort = KwisprSettings::fromEnv(invalidPortEnv);
    errors.clear();
    QVERIFY(!invalidPort.validate(&errors));
    QCOMPARE(invalidPortEnv.value(QStringLiteral("KWISPR_LOCAL_STT_PORT")), QStringLiteral("not-a-port"));
}

void SettingsModelTest::modelDirResolutionUsesExplicitPathsOrStandardDefault()
{
    KwisprSettings settings;
    const QString defaultPath = QDir(QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation))
                                    .filePath(QStringLiteral("kwispr/models"));
    QCOMPARE(settings.resolvedModelDir(), QDir::cleanPath(QFileInfo(defaultPath).absoluteFilePath()));

    settings.modelDir = QStringLiteral("   ");
    QCOMPARE(settings.resolvedModelDir(), QDir::cleanPath(QFileInfo(defaultPath).absoluteFilePath()));

    settings.modelDir = QStringLiteral("~");
    QCOMPARE(settings.resolvedModelDir(), QDir::cleanPath(QDir::homePath()));

    settings.modelDir = QStringLiteral("~/kwispr/../models");
    QCOMPARE(settings.resolvedModelDir(), QDir::cleanPath(QDir::home().filePath(QStringLiteral("models"))));

    const QString absolutePath = QDir::temp().filePath(QStringLiteral("kwispr/../models"));
    settings.modelDir = absolutePath;
    QCOMPARE(settings.resolvedModelDir(), QDir::cleanPath(absolutePath));

    settings.modelDir = QStringLiteral("relative/../models");
    QCOMPARE(settings.resolvedModelDir(),
             QDir::cleanPath(QFileInfo(QStringLiteral("relative/../models")).absoluteFilePath()));

    EnvFile env;
    env.setValue(QStringLiteral("KWISPR_MODEL_DIR"), QStringLiteral("~/saved-models"));
    const KwisprSettings loaded = KwisprSettings::fromEnv(env);
    QCOMPARE(loaded.modelDir, QDir::cleanPath(QDir::home().filePath(QStringLiteral("saved-models"))));

    EnvFile saved;
    loaded.writeTo(saved);
    QCOMPARE(saved.value(QStringLiteral("KWISPR_MODEL_DIR")), loaded.modelDir);
}

void SettingsModelTest::openAiPresetValidatesApiKeyForOfficialEndpoint()
{
    KwisprSettings settings;
    settings.applyOpenAiPreset("", "whisper-1", "en");

    QStringList errors;
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains("API key"));

    settings.apiKey = "sk-real-key";
    errors.clear();
    QVERIFY(settings.validate(&errors));

    settings.apiUrl = "http://127.0.0.1:19650/v1/audio/transcriptions";
    settings.apiKey.clear();
    errors.clear();
    QVERIFY(settings.validate(&errors));
}

void SettingsModelTest::openRouterPresetWritesChatBackendKeys()
{
    KwisprSettings settings;
    settings.applyOpenRouterPreset("sk-or-key", "openai/gpt-4o-mini-transcribe", "Clean this transcript", "wav");

    EnvFile env;
    settings.writeTo(env);

    QCOMPARE(env.value("KWISPR_BACKEND"), QString("openrouter-chat"));
    QCOMPARE(env.value("KWISPR_API_URL"), QString("https://openrouter.ai/api/v1/chat/completions"));
    QCOMPARE(env.value("KWISPR_API_KEY"), QString("sk-or-key"));
    QCOMPARE(env.value("KWISPR_MODEL"), QString("openai/gpt-4o-mini-transcribe"));
    QCOMPARE(env.value("KWISPR_AUDIO_FORMAT"), QString("wav"));
    QCOMPARE(env.value("KWISPR_TRANSCRIPTION_PROMPT"), QString("Clean this transcript"));
    QCOMPARE(env.value("KWISPR_HTTP_REFERER"), QString("https://github.com/blockedby/kwispr"));
    QCOMPARE(env.value("KWISPR_OPENROUTER_HTTP_REFERER"), QString("https://github.com/blockedby/kwispr"));
    QCOMPARE(env.value("KWISPR_APP_TITLE"), QString("KDE Whisper"));
    QCOMPARE(env.value("KWISPR_OPENROUTER_APP_TITLE"), QString("KDE Whisper"));
}

void SettingsModelTest::openRouterMetadataEnvUsesCanonicalKeysWithLegacyCompatibility()
{
    EnvFile conflicting;
    conflicting.setValue(QStringLiteral("KWISPR_HTTP_REFERER"), QStringLiteral("https://canonical.example"));
    conflicting.setValue(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"), QStringLiteral("https://legacy.example"));
    conflicting.setValue(QStringLiteral("KWISPR_APP_TITLE"), QStringLiteral("Canonical title"));
    conflicting.setValue(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"), QStringLiteral("Legacy title"));
    const KwisprSettings canonical = KwisprSettings::fromEnv(conflicting);
    QCOMPARE(canonical.openRouterReferer, QStringLiteral("https://canonical.example"));
    QCOMPARE(canonical.openRouterAppTitle, QStringLiteral("Canonical title"));

    EnvFile legacyOnly;
    legacyOnly.setValue(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"), QStringLiteral("https://legacy.example"));
    legacyOnly.setValue(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"), QStringLiteral("Legacy title"));
    const KwisprSettings legacy = KwisprSettings::fromEnv(legacyOnly);
    QCOMPARE(legacy.openRouterReferer, QStringLiteral("https://legacy.example"));
    QCOMPARE(legacy.openRouterAppTitle, QStringLiteral("Legacy title"));

    EnvFile canonicalBlank;
    canonicalBlank.setValue(QStringLiteral("KWISPR_HTTP_REFERER"), QString());
    canonicalBlank.setValue(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"), QStringLiteral("https://stale.example"));
    canonicalBlank.setValue(QStringLiteral("KWISPR_APP_TITLE"), QString());
    canonicalBlank.setValue(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"), QStringLiteral("Stale title"));
    const KwisprSettings blank = KwisprSettings::fromEnv(canonicalBlank);
    QCOMPARE(blank.openRouterReferer, QString());
    QCOMPARE(blank.openRouterAppTitle, QString());

    KwisprSettings cleared;
    cleared.openRouterReferer.clear();
    cleared.openRouterAppTitle.clear();
    cleared.writeTo(conflicting);
    QCOMPARE(conflicting.value(QStringLiteral("KWISPR_HTTP_REFERER")), QString());
    QCOMPARE(conflicting.value(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER")), QString());
    QCOMPARE(conflicting.value(QStringLiteral("KWISPR_APP_TITLE")), QString());
    QCOMPARE(conflicting.value(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE")), QString());
}

void SettingsModelTest::pasteHotkeyValidationAllowsOnlySupportedValues()
{
    KwisprSettings settings;
    settings.applyLocalPreset("whisper-large-v3-turbo", QString(), QString());

    for (const QString &hotkey : {QString("ctrl-v"), QString("ctrl-shift-v"), QString("shift-insert")}) {
        settings.pasteHotkey = hotkey;
        QStringList errors;
        QVERIFY2(settings.validate(&errors), qPrintable(errors.join('\n')));
    }

    settings.pasteHotkey = "meta-v";
    QStringList errors;
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains("paste hotkey"));
}

void SettingsModelTest::vadDefaultsMatchEnergyRuntime()
{
    const KwisprSettings settings;
    QCOMPARE(settings.vadProvider, QStringLiteral("energy"));
    QCOMPARE(settings.vadThreshold, 0.01);
}

void SettingsModelTest::vadEnvUsesCanonicalKeyWithLegacyCompatibility()
{
    KwisprSettings settings;
    settings.vadEnabled = true;

    EnvFile env;
    env.setValue(QStringLiteral("KWISPR_VAD"), QStringLiteral("0"));
    settings.writeTo(env);
    QCOMPARE(env.value(QStringLiteral("KWISPR_VAD_ENABLED")), QStringLiteral("1"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_VAD")), QStringLiteral("1"));

    EnvFile conflicting;
    conflicting.setValue(QStringLiteral("KWISPR_VAD_ENABLED"), QStringLiteral("0"));
    conflicting.setValue(QStringLiteral("KWISPR_VAD"), QStringLiteral("1"));
    QVERIFY(!KwisprSettings::fromEnv(conflicting).vadEnabled);

    EnvFile legacyOnly;
    legacyOnly.setValue(QStringLiteral("KWISPR_VAD"), QStringLiteral("1"));
    QVERIFY(KwisprSettings::fromEnv(legacyOnly).vadEnabled);
}

void SettingsModelTest::vadValidationMatchesRuntimeRequirements()
{
    KwisprSettings settings;
    settings.applyLocalPreset("whisper-large-v3-turbo", QString(), QString());
    settings.vadEnabled = true;
    settings.vadProvider = "silero";
    settings.vadModelPath.clear();

    QStringList errors;
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains("Silero"));

    settings.vadModelPath = "/models/silero.onnx";
    settings.vadThreshold = -0.1;
    errors.clear();
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains("threshold"));

    settings.vadThreshold = 0.5;
    settings.vadFrameMs = 0;
    errors.clear();
    QVERIFY(!settings.validate(&errors));
    QVERIFY(errors.join('\n').contains("frame"));

    settings.vadFrameMs = 30;
    errors.clear();
    QVERIFY(settings.validate(&errors));
}

QTEST_MAIN(SettingsModelTest)
#include "settings_model_test.moc"
