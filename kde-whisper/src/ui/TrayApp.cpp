#include "ui/TrayApp.h"

#include "config/KwisprSettings.h"
#include "config/EnvFile.h"
#include "models/ModelCatalog.h"
#include "models/ModelManager.h"
#include "runtime/KwisprController.h"
#include "runtime/LocalSttClient.h"
#include "runtime/LocalSttProcess.h"
#include "runtime/ProcessRunner.h"
#include "ui/SettingsDialog.h"

#include <KStatusNotifierItem>

#include <QApplication>
#include <QDir>
#include <QFileInfo>
#include <QMessageBox>
#include <QUrl>

namespace {
QString defaultModelDir()
{
    return QDir::home().filePath(QStringLiteral(".local/share/kwispr/models"));
}

bool envEnabled(const QString &value, bool fallback)
{
    if (value.trimmed().isEmpty()) {
        return fallback;
    }
    const QString normalized = value.trimmed().toLower();
    return normalized != QLatin1String("0") && normalized != QLatin1String("false") && normalized != QLatin1String("no");
}

KwisprSettings settingsFromEnv(const EnvFile &env)
{
    KwisprSettings settings;
    settings.backend = env.value(QStringLiteral("KWISPR_BACKEND"), settings.backend);
    settings.apiUrl = env.value(QStringLiteral("KWISPR_API_URL"), settings.apiUrl);
    settings.apiKey = env.value(QStringLiteral("KWISPR_API_KEY"), settings.apiKey);
    settings.model = env.value(QStringLiteral("KWISPR_MODEL"), settings.model);
    settings.language = env.value(QStringLiteral("KWISPR_LANGUAGE"), settings.language);
    settings.modelDir = env.value(QStringLiteral("KWISPR_MODEL_DIR"), defaultModelDir());
    settings.audioFormat = env.value(QStringLiteral("KWISPR_AUDIO_FORMAT"), settings.audioFormat);
    settings.transcriptionPrompt = env.value(QStringLiteral("KWISPR_TRANSCRIPTION_PROMPT"), settings.transcriptionPrompt);
    settings.openRouterReferer = env.value(QStringLiteral("KWISPR_OPENROUTER_HTTP_REFERER"), settings.openRouterReferer);
    settings.openRouterAppTitle = env.value(QStringLiteral("KWISPR_OPENROUTER_APP_TITLE"), settings.openRouterAppTitle);
    settings.autopaste = envEnabled(env.value(QStringLiteral("KWISPR_AUTOPASTE")), settings.autopaste);
    settings.pasteHotkey = env.value(QStringLiteral("KWISPR_PASTE_HOTKEY"), settings.pasteHotkey);
    bool ok = false;
    const double delay = env.value(QStringLiteral("KWISPR_AUTOPASTE_DELAY")).toDouble(&ok);
    if (ok) {
        settings.autopasteDelay = delay;
    }
    settings.sounds = envEnabled(env.value(QStringLiteral("KWISPR_SOUNDS")), settings.sounds);
    settings.pulseSource = env.value(QStringLiteral("KWISPR_PULSE_SOURCE"), settings.pulseSource);
    settings.vadEnabled = envEnabled(env.value(QStringLiteral("KWISPR_VAD"), env.value(QStringLiteral("KWISPR_VAD_ENABLED"))), settings.vadEnabled);
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
    return settings;
}
}

TrayApp::TrayApp(QString repoRoot, QString cacheDir, QObject *parent)
    : QObject(parent)
    , m_repoRoot(std::move(repoRoot))
    , m_cacheDir(std::move(cacheDir))
    , m_controller(std::make_unique<TrayController>(this, m_cacheDir, this))
    , m_notifier(new KStatusNotifierItem(QStringLiteral("org.kwispr.KdeWhisper"), this))
{
    m_notifier->setTitle(QStringLiteral("KDE Whisper"));
    m_notifier->setIconByName(QStringLiteral("audio-input-microphone"));
    m_notifier->setCategory(KStatusNotifierItem::ApplicationStatus);
    m_notifier->setContextMenu(m_controller->menu());
    m_notifier->setStatus(KStatusNotifierItem::Active);
}

TrayApp::~TrayApp() = default;

void TrayApp::toggleRecording()
{
    ProcessRunner runner;
    KwisprController controller(m_repoRoot, &runner);
    const ProcessResult result = controller.toggleRecording();
    if (result.exitCode != 0) {
        QMessageBox::warning(nullptr, QStringLiteral("KDE Whisper"), result.stderrText.isEmpty() ? QStringLiteral("Toggle recording failed.") : result.stderrText);
    }
}

void TrayApp::openSettings()
{
    const QString envPath = m_repoRoot + QStringLiteral("/.env");
    EnvFile env;
    env.load(envPath);
    KwisprSettings settings = settingsFromEnv(env);
    if (settings.modelDir.trimmed().isEmpty()) {
        settings.modelDir = defaultModelDir();
    }
    const QString catalogPath = m_repoRoot + QStringLiteral("/models/local-stt-catalog.json");
    const ModelCatalog catalog = ModelCatalog::load(catalogPath);

    ProcessRunner runner;
    ModelManager modelManager(m_repoRoot, catalogPath, settings.modelDir, &runner);
    const QMap<QString, bool> statusById = modelManager.listInstalledStatus();
    QStringList installedModelIds;
    for (auto it = statusById.cbegin(); it != statusById.cend(); ++it) {
        if (it.value()) {
            installedModelIds.append(it.key());
        }
    }

    SettingsDialog dialog(settings, catalog, installedModelIds, &env, &modelManager);
    connect(&dialog, &SettingsDialog::settingsSaved, this, [&env, envPath]() {
        env.save(envPath);
    });
    dialog.exec();
}

void TrayApp::startLocalStt()
{
    ProcessRunner runner;
    LocalSttProcess process(m_repoRoot, &runner);
    const ProcessResult result = process.start();
    if (result.exitCode != 0) {
        QMessageBox::warning(nullptr, QStringLiteral("KDE Whisper"), result.stderrText.isEmpty() ? QStringLiteral("Failed to start local STT.") : result.stderrText);
    }
    m_controller->refreshState();
}

void TrayApp::stopLocalStt()
{
    QMessageBox::information(nullptr, QStringLiteral("KDE Whisper"), QStringLiteral("Stopping managed local STT processes will be implemented in a later task."));
    m_controller->refreshState();
}

void TrayApp::downloadVerifyModels()
{
    QMessageBox::information(nullptr, QStringLiteral("KDE Whisper"), QStringLiteral("Model download/verify UI will be implemented in a later task."));
}

void TrayApp::retryLastFailed()
{
    const QString path = m_cacheDir + QStringLiteral("/last-failed.txt");
    ProcessRunner runner;
    KwisprController controller(m_repoRoot, &runner);
    const ProcessResult result = controller.retry(path);
    if (result.exitCode != 0) {
        QMessageBox::warning(nullptr, QStringLiteral("KDE Whisper"), result.stderrText.isEmpty() ? QStringLiteral("Retry failed.") : result.stderrText);
    }
    m_controller->refreshState();
}

void TrayApp::quitApplication()
{
    qApp->quit();
}

LocalSttState TrayApp::localSttState() const
{
    LocalSttClient client(QUrl(QStringLiteral("http://127.0.0.1:9000")));
    return client.checkHealth(500).state;
}
