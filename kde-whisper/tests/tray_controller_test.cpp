#include "ui/TrayController.h"

#include <QtTest/QtTest>
#include <QAction>
#include <QMenu>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QFile>

class FakeTrayActions : public ITrayActions {
public:
    int toggleCalls = 0;
    int settingsCalls = 0;
    int startCalls = 0;
    int stopCalls = 0;
    int retryCalls = 0;
    int quitCalls = 0;
    LocalSttState state = LocalSttState::Stopped;

    void toggleRecording() override { ++toggleCalls; }
    void openSettings() override { ++settingsCalls; }
    void startLocalStt() override { ++startCalls; state = LocalSttState::Healthy; }
    void stopLocalStt() override { ++stopCalls; state = LocalSttState::Stopped; }
    void retryLastFailed() override { ++retryCalls; }
    void quitApplication() override { ++quitCalls; }
    LocalSttState localSttState() const override { return state; }
};

class TrayControllerTest : public QObject {
    Q_OBJECT
private slots:
    void menuContainsRequiredActions();
    void actionsCallInjectedServices();
    void retryEnabledOnlyWhenLastFailedExists();
    void localSttActionsReflectStatus();
};

static QAction *actionByText(QMenu *menu, const QString &text)
{
    for (QAction *action : menu->actions()) {
        if (action->text() == text) {
            return action;
        }
    }
    return nullptr;
}

void TrayControllerTest::menuContainsRequiredActions()
{
    QTemporaryDir cacheDir;
    QVERIFY(cacheDir.isValid());
    FakeTrayActions actions;
    TrayController controller(&actions, cacheDir.path());
    QMenu *menu = controller.menu();

    QVERIFY(actionByText(menu, QStringLiteral("Toggle Recording")));
    QVERIFY(actionByText(menu, QStringLiteral("Settings")));
    QVERIFY(actionByText(menu, QStringLiteral("Start Local STT")));
    QVERIFY(actionByText(menu, QStringLiteral("Stop Local STT")));
    QVERIFY(actionByText(menu, QStringLiteral("Manage Local Models")));
    QVERIFY(!actionByText(menu, QStringLiteral("Download/Verify Models")));
    QVERIFY(actionByText(menu, QStringLiteral("Retry Last Failed")));
    QVERIFY(actionByText(menu, QStringLiteral("Quit")));
}

void TrayControllerTest::actionsCallInjectedServices()
{
    QTemporaryDir cacheDir;
    QVERIFY(cacheDir.isValid());
    QFile lastFailed(cacheDir.filePath(QStringLiteral("last-failed.txt")));
    QVERIFY(lastFailed.open(QIODevice::WriteOnly));
    lastFailed.write("retry");
    lastFailed.close();

    FakeTrayActions actions;
    TrayController controller(&actions, cacheDir.path());
    QMenu *menu = controller.menu();

    actionByText(menu, QStringLiteral("Toggle Recording"))->trigger();
    actionByText(menu, QStringLiteral("Settings"))->trigger();
    actionByText(menu, QStringLiteral("Start Local STT"))->trigger();
    actionByText(menu, QStringLiteral("Stop Local STT"))->trigger();
    actionByText(menu, QStringLiteral("Manage Local Models"))->trigger();
    actionByText(menu, QStringLiteral("Retry Last Failed"))->trigger();
    actionByText(menu, QStringLiteral("Quit"))->trigger();

    QCOMPARE(actions.toggleCalls, 1);
    QCOMPARE(actions.settingsCalls, 2);
    QCOMPARE(actions.startCalls, 1);
    QCOMPARE(actions.stopCalls, 1);
    QCOMPARE(actions.retryCalls, 1);
    QCOMPARE(actions.quitCalls, 1);
}

void TrayControllerTest::retryEnabledOnlyWhenLastFailedExists()
{
    QTemporaryDir cacheDir;
    QVERIFY(cacheDir.isValid());
    FakeTrayActions actions;
    TrayController controller(&actions, cacheDir.path());

    QAction *retry = actionByText(controller.menu(), QStringLiteral("Retry Last Failed"));
    QVERIFY(retry);
    QVERIFY(!retry->isEnabled());

    QFile lastFailed(cacheDir.filePath(QStringLiteral("last-failed.txt")));
    QVERIFY(lastFailed.open(QIODevice::WriteOnly));
    lastFailed.write("retry");
    lastFailed.close();
    controller.refreshState();
    QVERIFY(retry->isEnabled());
}

void TrayControllerTest::localSttActionsReflectStatus()
{
    QTemporaryDir cacheDir;
    QVERIFY(cacheDir.isValid());
    FakeTrayActions actions;
    actions.state = LocalSttState::Stopped;
    TrayController controller(&actions, cacheDir.path());

    QAction *start = actionByText(controller.menu(), QStringLiteral("Start Local STT"));
    QAction *stop = actionByText(controller.menu(), QStringLiteral("Stop Local STT"));
    QVERIFY(start->isEnabled());
    QVERIFY(!stop->isEnabled());

    actions.state = LocalSttState::Healthy;
    controller.refreshState();
    QVERIFY(!start->isEnabled());
    QVERIFY(stop->isEnabled());
}

QTEST_MAIN(TrayControllerTest)
#include "tray_controller_test.moc"
