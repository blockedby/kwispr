#pragma once

#include "runtime/ProcessRunner.h"
#include "ui/GlobalShortcut.h"
#include "ui/TrayController.h"

#include <QObject>
#include <QPointer>
#include <QString>
#include <memory>

class KStatusNotifierItem;
class KwisprController;
class SettingsDialog;
class ModelManager;

class TrayApp : public QObject, public ITrayActions
{
    Q_OBJECT
public:
    explicit TrayApp(QString repoRoot,
                     QString cacheDir,
                     QObject *parent = nullptr,
                     std::unique_ptr<IGlobalShortcutBackend> shortcutBackend = {},
                     std::unique_ptr<ProcessRunner> recordingRunner = {});
    ~TrayApp() override;

    void toggleRecording() override;
    void openSettings() override;
    void startLocalStt() override;
    void stopLocalStt() override;
    void retryLastFailed() override;
    void quitApplication() override;
    LocalSttState localSttState() const override;

private:
    QString m_repoRoot;
    QString m_cacheDir;
    std::unique_ptr<ProcessRunner> m_recordingRunner;
    std::unique_ptr<GlobalShortcutManager> m_globalShortcut;
    std::unique_ptr<TrayController> m_controller;
    KStatusNotifierItem *m_notifier = nullptr;
    QPointer<SettingsDialog> m_settingsDialog;
};
