#include "ui/SettingsDialog.h"
#include "ui/TrayApp.h"

#include <QApplication>
#include <QPointer>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest/QtTest>

namespace {
QList<SettingsDialog *> openSettingsDialogs()
{
    QList<SettingsDialog *> dialogs;
    for (QWidget *widget : QApplication::topLevelWidgets()) {
        if (auto *dialog = qobject_cast<SettingsDialog *>(widget)) {
            dialogs.append(dialog);
        }
    }
    return dialogs;
}
}

class TrayAppTest : public QObject
{
    Q_OBJECT
private slots:
    void repeatedOpenSettingsReusesAndActivatesDialogDuringModalExec();
};

void TrayAppTest::repeatedOpenSettingsReusesAndActivatesDialogDuringModalExec()
{
    QTemporaryDir tempDir;
    QVERIFY(tempDir.isValid());

    const bool hadConfigOverride = qEnvironmentVariableIsSet("KWISPR_CONFIG_FILE");
    const QByteArray previousConfigOverride = qgetenv("KWISPR_CONFIG_FILE");
    qputenv("KWISPR_CONFIG_FILE", tempDir.filePath(QStringLiteral("config.env")).toUtf8());

    // Keep this tray fixture for the standalone test process; notifier teardown is outside
    // this modal-dialog regression.
    auto *tray = new TrayApp(tempDir.path(), tempDir.path());
    QPointer<SettingsDialog> firstDialog;
    int dialogCountBeforeSecondCall = 0;
    int dialogCountDuringSecondCall = 0;
    bool firstCallIsModal = false;
    bool minimizedBeforeSecondCall = false;
    bool reusedFirstDialog = false;
    bool restoredFirstDialog = false;
    bool activatedFirstDialog = false;

    QTimer::singleShot(0, tray, [&]() {
        const QList<SettingsDialog *> initialDialogs = openSettingsDialogs();
        dialogCountBeforeSecondCall = initialDialogs.size();
        if (initialDialogs.isEmpty()) {
            return;
        }

        firstDialog = initialDialogs.constFirst();
        firstCallIsModal = firstDialog->isModal();
        firstDialog->showMinimized();
        minimizedBeforeSecondCall = firstDialog->isMinimized();

        QTimer::singleShot(0, tray, [&]() {
            const QList<SettingsDialog *> dialogs = openSettingsDialogs();
            dialogCountDuringSecondCall = dialogs.size();
            reusedFirstDialog = dialogs.size() == 1 && dialogs.constFirst() == firstDialog;
            restoredFirstDialog = firstDialog && !firstDialog->isMinimized() && firstDialog->isVisible();
            activatedFirstDialog = firstDialog && firstDialog->isActiveWindow();
            for (SettingsDialog *dialog : dialogs) {
                dialog->reject();
            }
        });

        tray->openSettings();
    });

    tray->openSettings();

    if (hadConfigOverride) {
        qputenv("KWISPR_CONFIG_FILE", previousConfigOverride);
    } else {
        qunsetenv("KWISPR_CONFIG_FILE");
    }

    QCOMPARE(dialogCountBeforeSecondCall, 1);
    QVERIFY(firstCallIsModal);
    QVERIFY(minimizedBeforeSecondCall);
    QCOMPARE(dialogCountDuringSecondCall, 1);
    QVERIFY(reusedFirstDialog);
    QVERIFY(restoredFirstDialog);
    QVERIFY(activatedFirstDialog);
    QVERIFY(firstDialog.isNull());
    QVERIFY(openSettingsDialogs().isEmpty());
}

QTEST_MAIN(TrayAppTest)
#include "tray_app_test.moc"
