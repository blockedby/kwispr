#include "ui/TrayController.h"

#include <QAction>
#include <QFileInfo>
#include <QMenu>

TrayController::TrayController(ITrayActions *actions, QString cacheDir, QObject *parent)
    : QObject(parent)
    , m_actions(actions)
    , m_cacheDir(std::move(cacheDir))
    , m_menu(new QMenu())
{
    addAction(QStringLiteral("Toggle Recording"), &ITrayActions::toggleRecording);
    m_meetingsAction = m_menu->addAction(QStringLiteral("Meetings…"));
    connect(m_meetingsAction, &QAction::triggered, this, [this] {
        if (m_actions) {
            m_actions->openMeetings();
        }
    });
    m_menu->addSeparator();
    addAction(QStringLiteral("Settings"), &ITrayActions::openSettings);
    m_menu->addSeparator();
    m_startLocalSttAction = addAction(QStringLiteral("Start Local STT"), &ITrayActions::startLocalStt);
    m_stopLocalSttAction = addAction(QStringLiteral("Stop Local STT"), &ITrayActions::stopLocalStt);
    addAction(QStringLiteral("Manage Local Models"), &ITrayActions::openSettings);
    m_retryLastFailedAction = addAction(QStringLiteral("Retry Last Failed"), &ITrayActions::retryLastFailed);
    m_menu->addSeparator();
    addAction(QStringLiteral("Quit"), &ITrayActions::quitApplication);
    refreshState();
}

TrayController::~TrayController()
{
    delete m_menu;
}

QMenu *TrayController::menu() const
{
    return m_menu;
}

void TrayController::setMeetingState(const QString &state)
{
    const bool recording = state == QStringLiteral("recording") || state == QStringLiteral("starting") || state == QStringLiteral("stopping");
    m_meetingsAction->setText(recording ? tr("Meetings — Recording…") : tr("Meetings…"));
    m_meetingsAction->setIcon(recording ? QIcon::fromTheme(QStringLiteral("media-record")) : QIcon());
}

void TrayController::refreshState()
{
    if (m_retryLastFailedAction) {
        m_retryLastFailedAction->setEnabled(hasLastFailed());
    }

    const bool localRunning = m_actions && m_actions->localSttState() == LocalSttState::Healthy;
    if (m_startLocalSttAction) {
        m_startLocalSttAction->setEnabled(!localRunning);
    }
    if (m_stopLocalSttAction) {
        m_stopLocalSttAction->setEnabled(localRunning);
    }
}

QAction *TrayController::addAction(const QString &text, void (ITrayActions::*method)())
{
    QAction *action = m_menu->addAction(text);
    connect(action, &QAction::triggered, this, [this, method]() {
        if (m_actions) {
            (m_actions->*method)();
            refreshState();
        }
    });
    return action;
}

bool TrayController::hasLastFailed() const
{
    return QFileInfo::exists(m_cacheDir + QStringLiteral("/last-failed.txt"));
}
