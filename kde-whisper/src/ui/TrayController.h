#pragma once

#include "runtime/LocalSttClient.h"

#include <QObject>
#include <QString>

class QAction;
class QMenu;

class ITrayActions
{
public:
    virtual ~ITrayActions() = default;

    virtual void toggleRecording() = 0;
    virtual void openSettings() = 0;
    virtual void startLocalStt() = 0;
    virtual void stopLocalStt() = 0;
    virtual void retryLastFailed() = 0;
    virtual void quitApplication() = 0;
    virtual LocalSttState localSttState() const = 0;
};

class TrayController : public QObject
{
    Q_OBJECT
public:
    explicit TrayController(ITrayActions *actions, QString cacheDir, QObject *parent = nullptr);
    ~TrayController() override;

    QMenu *menu() const;
    void refreshState();

private:
    QAction *addAction(const QString &text, void (ITrayActions::*method)());
    bool hasLastFailed() const;

    ITrayActions *m_actions;
    QString m_cacheDir;
    QMenu *m_menu = nullptr;
    QAction *m_startLocalSttAction = nullptr;
    QAction *m_stopLocalSttAction = nullptr;
    QAction *m_retryLastFailedAction = nullptr;
};
