#include "ui/TrayApp.h"

#include "config/KwisprSettings.h"
#include "config/EnvFile.h"
#include "models/ModelCatalog.h"
#include "models/ModelManager.h"
#include "runtime/KwisprController.h"
#include "runtime/LocalSttClient.h"
#include "runtime/ProcessRunner.h"
#include "runtime/RetryState.h"
#include "ui/SettingsDialog.h"

#include <KStatusNotifierItem>

#include <QApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QMessageBox>
#include <QStandardPaths>
#include <QUrl>

namespace {
QString configFilePath()
{
    const QString overridePath = qEnvironmentVariable("KWISPR_CONFIG_FILE").trimmed();
    if (!overridePath.isEmpty()) {
        return QFileInfo(overridePath).absoluteFilePath();
    }
    return QDir(QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation))
        .filePath(QStringLiteral("kwispr/config.env"));
}

void loadConfigWithLegacyMigration(EnvFile *env, const QString &configPath, const QString &legacyPath)
{
    if (env->load(configPath)) {
        return;
    }
    if (!QFileInfo::exists(legacyPath) || !env->load(legacyPath)) {
        return;
    }

    QDir().mkpath(QFileInfo(configPath).absolutePath());
    env->save(configPath);
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
    const QString envPath = configFilePath();
    EnvFile env;
    loadConfigWithLegacyMigration(&env, envPath, m_repoRoot + QStringLiteral("/.env"));
    KwisprSettings settings = KwisprSettings::fromEnv(env);
    const QString resolvedModelDir = settings.resolvedModelDir();
    settings.modelDir = resolvedModelDir;
    const QString catalogPath = m_repoRoot + QStringLiteral("/models/local-stt-catalog.json");
    const ModelCatalog catalog = ModelCatalog::load(catalogPath);

    ProcessRunner runner;
    ModelManager modelManager(m_repoRoot, catalogPath, resolvedModelDir, &runner);
    const QMap<QString, bool> statusById = modelManager.listInstalledStatus();
    QStringList installedModelIds;
    for (auto it = statusById.cbegin(); it != statusById.cend(); ++it) {
        if (it.value()) {
            installedModelIds.append(it.key());
        }
    }

    const QString servicePath = QDir(QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation))
        .filePath(QStringLiteral("systemd/user/kwispr-local-stt.service"));
    const bool localRuntimeInstalled = QFileInfo::exists(servicePath);
    QString previousLocalSttHost = settings.localSttHost;
    int previousLocalSttPort = settings.localSttPort;
    SettingsDialog dialog(settings, catalog, installedModelIds, &env, &modelManager,
                          localRuntimeInstalled);
    connect(&dialog, &SettingsDialog::settingsSaved, this,
            [this, &env, envPath, localRuntimeInstalled, previousLocalSttHost, previousLocalSttPort](const KwisprSettings &savedSettings) mutable {
        QDir().mkpath(QFileInfo(envPath).absolutePath());
        if (!env.save(envPath)) {
            QMessageBox::warning(nullptr,
                                 QStringLiteral("KDE Whisper"),
                                 QStringLiteral("Could not save settings to %1: %2")
                                     .arg(envPath, env.errorString()));
            return;
        }
        const bool serverChanged = savedSettings.localSttHost != previousLocalSttHost
            || savedSettings.localSttPort != previousLocalSttPort;
        if (localRuntimeInstalled && serverChanged) {
            ProcessRunner restartRunner;
            const ProcessResult result = restartRunner.run(
                QStringLiteral("systemctl"),
                {QStringLiteral("--user"), QStringLiteral("restart"), QStringLiteral("kwispr-local-stt.service")});
            if (result.exitCode != 0) {
                QMessageBox::warning(nullptr, QStringLiteral("KDE Whisper"),
                                     result.stderrText.isEmpty() ? QStringLiteral("Local STT settings saved, but the service could not be restarted.")
                                                                  : result.stderrText);
            } else {
                QMessageBox::information(nullptr, QStringLiteral("KDE Whisper"),
                                         QStringLiteral("Local STT listen settings applied and the service restarted."));
            }
        }
        previousLocalSttHost = savedSettings.localSttHost;
        previousLocalSttPort = savedSettings.localSttPort;
        m_controller->refreshState();
    });
    dialog.exec();
}

void TrayApp::startLocalStt()
{
    ProcessRunner runner;
    const ProcessResult result = runner.run(
        QStringLiteral("systemctl"),
        {QStringLiteral("--user"), QStringLiteral("start"), QStringLiteral("kwispr-local-stt.service")});
    if (result.exitCode != 0) {
        QMessageBox::warning(
            nullptr,
            QStringLiteral("KDE Whisper"),
            result.stderrText.isEmpty()
                ? QStringLiteral("Failed to start local STT. Install the local runtime first.")
                : result.stderrText);
    }
    m_controller->refreshState();
}

void TrayApp::stopLocalStt()
{
    ProcessRunner runner;
    const ProcessResult result = runner.run(
        QStringLiteral("systemctl"),
        {QStringLiteral("--user"), QStringLiteral("stop"), QStringLiteral("kwispr-local-stt.service")});
    if (result.exitCode != 0) {
        QMessageBox::warning(nullptr,
                             QStringLiteral("KDE Whisper"),
                             result.stderrText.isEmpty() ? QStringLiteral("Failed to stop local STT.")
                                                         : result.stderrText);
    }
    m_controller->refreshState();
}

void TrayApp::retryLastFailed()
{
    const QString statePath = m_cacheDir + QStringLiteral("/last-failed.txt");
    QFile stateFile(statePath);
    if (!stateFile.open(QIODevice::ReadOnly | QIODevice::Text)) {
        QMessageBox::warning(nullptr, QStringLiteral("KDE Whisper"), QStringLiteral("No failed recording is available to retry."));
        return;
    }

    const QString wavPath = retryWavPathFromState(QString::fromUtf8(stateFile.readAll()));

    ProcessRunner runner;
    KwisprController controller(m_repoRoot, &runner);
    const ProcessResult result = controller.retry(wavPath);
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
    EnvFile env;
    KwisprSettings settings;
    const QString envPath = configFilePath();
    if (env.load(envPath)) {
        settings = KwisprSettings::fromEnv(env);
    }
    LocalSttClient client(settings.localSttHealthUrl());
    return client.checkHealth(500).state;
}
