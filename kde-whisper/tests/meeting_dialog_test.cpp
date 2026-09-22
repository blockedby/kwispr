#include "ui/MeetingDialog.h"
#include "config/EnvFile.h"

#include <QAbstractItemView>
#include <QComboBox>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QProcess>
#include <QScrollArea>
#include <QScrollBar>
#include <QSignalSpy>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest/QtTest>
#include <memory>

namespace {
bool writeFile(const QString &path, const QByteArray &contents)
{
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(contents) == contents.size();
}

class WorkerFixture
{
public:
    QTemporaryDir dir;

    bool create()
    {
        const QByteArray worker = R"PY(#!/usr/bin/env python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\n')
state_file = root / 'state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {'state': 'idle'}
if args[0] == 'sources':
    if (root / 'sources.json').exists():
        print((root / 'sources.json').read_text())
        sys.exit(0)
    if (root / 'sources-fail').exists():
        print(json.dumps({'state':'error', 'message':'PulseAudio unavailable'}), file=sys.stderr)
        sys.exit(1)
    devices = [] if (root / 'sources-empty').exists() else [{'name':'mic.fixture', 'description':'USB headset microphone'}]
    print(json.dumps({'microphones': devices, 'monitors':[{'name':'sink.fixture.monitor','description':'Call headphones'}], 'default_microphone':'mic.fixture', 'default_monitor':'sink.fixture.monitor'}))
elif args[0] == 'status':
    if (root / 'slow-status').exists():
        time.sleep(1.2)
    print(json.dumps(state))
elif args[0] == 'start':
    time.sleep(0.25)
    if (root / 'start-fail').exists():
        print(json.dumps({'state':'error', 'message':'Cannot record selected microphone'}), file=sys.stderr)
        sys.exit(1)
    opts = dict(zip(args[1::2], args[2::2]))
    session = root / 'saved meeting'
    session.mkdir(exist_ok=True)
    state = {'state':'recording','session_dir':str(session),'message':'','mic_source':opts['--mic'],'monitor_source':opts['--monitor'],'started_at':'2026-09-21T10:00:00Z'}
    state_file.write_text(json.dumps(state))
    print(json.dumps(state))
elif args[0] == 'stop':
    state['state'] = 'processing'
    state_file.write_text(json.dumps(state))
    print(json.dumps(state))
elif args[0] == 'process':
    state['state'] = 'complete'
    state['message'] = 'Transcript saved successfully.'
    state['transcript_path'] = str(root / 'saved meeting' / 'transcript.md')
    state_file.write_text(json.dumps(state))
    print(json.dumps(state))
)PY";
        const QByteArray setup = R"PY(#!/usr/bin/env python3
import pathlib, sys, time
root = pathlib.Path(__file__).parent
print('Downloading fixture meeting model', flush=True)
time.sleep(.25)
if (root / 'setup-fail').exists():
    print('Fixture download failed', flush=True)
    sys.exit(1)
print('Fixture meeting models ready', flush=True)
)PY";
        if (!dir.isValid() || !writeFile(dir.filePath(QStringLiteral("kwispr-meetings.py")), worker)
            || !writeFile(dir.filePath(QStringLiteral("kwispr-meetings-setup.py")), setup)) {
            return false;
        }
        return QFile::setPermissions(dir.filePath(QStringLiteral("kwispr-meetings.py")), QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner)
            && QFile::setPermissions(dir.filePath(QStringLiteral("kwispr-meetings-setup.py")), QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner);
    }

    QString configPath() const { return dir.filePath(QStringLiteral("config.env")); }

    QList<QJsonArray> calls(const QString &command) const
    {
        QFile file(dir.filePath(QStringLiteral("calls.jsonl")));
        if (!file.open(QIODevice::ReadOnly)) return {};
        QList<QJsonArray> result;
        for (const auto &line : file.readAll().split('\n')) {
            const auto args = QJsonDocument::fromJson(line).array();
            if (!args.isEmpty() && args.first().toString() == command) result.append(args);
        }
        return result;
    }

    bool setState(const QString &state, const QString &message = QString())
    {
        return writeFile(dir.filePath(QStringLiteral("state.json")), QJsonDocument(QJsonObject{
            {QStringLiteral("state"), state},
            {QStringLiteral("session_dir"), dir.filePath(QStringLiteral("saved meeting"))},
            {QStringLiteral("message"), message},
        }).toJson());
    }
};

template <typename T> T *control(MeetingDialog &dialog, const char *name)
{
    return dialog.findChild<T *>(QString::fromLatin1(name));
}
}

class MeetingDialogTest : public QObject
{
    Q_OBJECT
private slots:
    void recordsAsynchronouslyPreservesConfigAndStopsBeforeClosing();
    void startFailurePreservesChoicesAndReportsWorkerMessage();
    void failedTranscriptionCanRetry();
    void missingSourcesDisableStartAndCanRefresh();
    void unavailableSavedSourceNeedsExplicitSelection();
    void deviceDefaultsAreAnnotatedWithoutChangingSavedSelections();
    void longSelectedDeviceNamesRemainVisible();
    void meetingLanguagesAreIndependentAndPersistOnStart();
    void retryPersistsLanguageCorrectionsWithoutChangingDeviceChoices();
    void invalidLanguageDoesNotStartRecording();
    void openSavedFolderUsesInjectedFileManagerAndReportsErrors();
    void disappearingWorkerDoesNotTrapWindowInStartingState();
    void modelSetupIsAsynchronousAndReportsFailure();
    void pollingDoesNotOverlapOrRunWhileHidden();
    void hiddenActiveMeetingKeepsPolling_data();
    void hiddenActiveMeetingKeepsPolling();
    void narrowLayoutKeepsActionsReachable();
    void destructionDisconnectsPendingWorkerCallbacks();
};

void MeetingDialogTest::destructionDisconnectsPendingWorkerCallbacks()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(fixture.setState(QStringLiteral("complete"), QStringLiteral("An allocated status message for teardown.")));
    auto dialog = std::make_unique<MeetingDialog>(fixture.dir.path(), fixture.configPath());
    dialog->show();
    QTRY_COMPARE(control<QLabel>(*dialog, "meetingStatus")->text(), QStringLiteral("Transcript saved"));
    QProcess *status = nullptr;
    for (auto *process : dialog->findChildren<QProcess *>()) {
        if (process->arguments().value(0) == QStringLiteral("status")) status = process;
    }
    QVERIFY(status);
    QTRY_COMPARE(status->state(), QProcess::NotRunning);
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("slow-status")), "1"));
    QTimer *poll = nullptr;
    for (auto *timer : dialog->findChildren<QTimer *>()) {
        if (!timer->isSingleShot() && timer->interval() == 1000) poll = timer;
    }
    QVERIFY(poll);
    poll->stop();
    QVERIFY(QMetaObject::invokeMethod(poll, "timeout", Qt::DirectConnection));
    QTRY_COMPARE(status->state(), QProcess::Running);
    QSignalSpy finished(status, &QProcess::finished);
    // Let the worker exit without delivering its queued completion to Qt.
    // QProcess destruction then emits finished after derived members would
    // already have been destroyed unless the dialog disconnects the callback.
    QTest::qSleep(1400);
    QCOMPARE(finished.count(), 0);
    dialog.reset();
    QCOMPARE(finished.count(), 1);
}

void MeetingDialogTest::recordsAsynchronouslyPreservesConfigAndStopsBeforeClosing()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.configPath(), "# keep this comment\nKWISPR_VOCABULARY='Kwispr|Codex'\nKWISPR_STOP_DELAY_MS=350\n"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    QSignalSpy stateSpy(&dialog, &MeetingDialog::meetingStateChanged);
    dialog.show();
    auto *start = control<QPushButton>(dialog, "meetingStart");
    auto *stop = control<QPushButton>(dialog, "meetingStop");
    QTRY_VERIFY(start->isEnabled());
    QCOMPARE(control<QComboBox>(dialog, "meetingMicrophone")->currentData().toString(), QStringLiteral("mic.fixture"));
    QCOMPARE(control<QComboBox>(dialog, "meetingMonitor")->currentData().toString(), QStringLiteral("sink.fixture.monitor"));
    control<QLineEdit>(dialog, "meetingTitle")->setText(QStringLiteral("A meeting; $(literal) 'title'"));
    const QString output = fixture.dir.filePath(QStringLiteral("Output with spaces"));
    control<QLineEdit>(dialog, "meetingOutput")->setText(output);
    control<QSpinBox>(dialog, "meetingSpeakers")->setValue(3);
    int timerTicks = 0;
    QTimer responsiveness;
    responsiveness.setInterval(10);
    connect(&responsiveness, &QTimer::timeout, &dialog, [&timerTicks] { ++timerTicks; });
    responsiveness.start();
    QElapsedTimer elapsed;
    elapsed.start();
    start->click();
    QVERIFY(elapsed.elapsed() < 100);
    QVERIFY(!start->isEnabled());
    start->click();
    QTRY_VERIFY(stop->isEnabled());
    QVERIFY(timerTicks > 5);
    const auto starts = fixture.calls(QStringLiteral("start"));
    QCOMPARE(starts.size(), 1);
    QCOMPARE(starts.first(), QJsonArray({QStringLiteral("start"), QStringLiteral("--mic"), QStringLiteral("mic.fixture"),
                                        QStringLiteral("--monitor"), QStringLiteral("sink.fixture.monitor"), QStringLiteral("--output-dir"), output,
                                        QStringLiteral("--speakers"), QStringLiteral("3"), QStringLiteral("--title"), QStringLiteral("A meeting; $(literal) 'title'")}));
    EnvFile saved;
    QVERIFY(saved.load(fixture.configPath()));
    QCOMPARE(saved.value(QStringLiteral("KWISPR_VOCABULARY")), QStringLiteral("Kwispr|Codex"));
    QCOMPARE(saved.value(QStringLiteral("KWISPR_STOP_DELAY_MS")), QStringLiteral("350"));
    QCOMPARE(saved.value(QStringLiteral("KWISPR_MEETING_OUTPUT_DIR")), output);
    QCOMPARE(saved.value(QStringLiteral("KWISPR_MEETING_SPEAKERS")), QStringLiteral("3"));
    QVERIFY(dialog.recordingActive());
    QVERIFY(dialog.windowTitle().contains(QStringLiteral("Recording")));
    QVERIFY(control<QLabel>(dialog, "meetingActiveSources")->text().contains(QStringLiteral("sink.fixture.monitor")));
    dialog.close();
    QVERIFY(dialog.isVisible());
    QTest::keyClick(&dialog, Qt::Key_Escape);
    QVERIFY(dialog.isVisible());
    stop->click();
    QTRY_VERIFY(!dialog.recordingActive());
    QCOMPARE(fixture.calls(QStringLiteral("stop")).size(), 1);
    QVERIFY(control<QLabel>(dialog, "meetingStatus")->text().contains(QStringLiteral("Transcribing")));
    QVERIFY(!start->isEnabled());
    dialog.close();
    QVERIFY(!dialog.isVisible());
    QVERIFY(stateSpy.count() >= 4);
}

void MeetingDialogTest::startFailurePreservesChoicesAndReportsWorkerMessage()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("start-fail")), "1"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    auto *start = control<QPushButton>(dialog, "meetingStart");
    QTRY_VERIFY(start->isEnabled());
    control<QLineEdit>(dialog, "meetingTitle")->setText(QStringLiteral("Weekly meeting"));
    start->click();
    QTRY_VERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("Cannot record selected microphone")));
    QTRY_VERIFY(start->isEnabled());
    QCOMPARE(control<QLineEdit>(dialog, "meetingTitle")->text(), QStringLiteral("Weekly meeting"));
    QVERIFY(!dialog.recordingActive());
}

void MeetingDialogTest::failedTranscriptionCanRetry()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(QDir().mkpath(fixture.dir.filePath(QStringLiteral("saved meeting"))));
    QVERIFY(fixture.setState(QStringLiteral("failed"), QStringLiteral("Missing meeting model. Set up meeting models, then retry.")));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    auto *retry = control<QPushButton>(dialog, "meetingRetry");
    QTRY_VERIFY(retry->isEnabled());
    QVERIFY(control<QLabel>(dialog, "meetingMessage")->text().contains(QStringLiteral("Missing meeting model")));
    retry->click();
    QTRY_COMPARE(control<QLabel>(dialog, "meetingStatus")->text(), QStringLiteral("Transcript saved"));
    QCOMPARE(fixture.calls(QStringLiteral("process")).first(), QJsonArray({QStringLiteral("process"), fixture.dir.filePath(QStringLiteral("saved meeting"))}));
    QVERIFY(control<QPushButton>(dialog, "meetingOpenFolder")->isEnabled());
    QVERIFY(!retry->isEnabled());
}

void MeetingDialogTest::missingSourcesDisableStartAndCanRefresh()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("sources-fail")), "1"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("PulseAudio unavailable")));
    QVERIFY(!control<QPushButton>(dialog, "meetingStart")->isEnabled());
    QVERIFY(QFile::remove(fixture.dir.filePath(QStringLiteral("sources-fail"))));
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("sources-empty")), "1"));
    control<QPushButton>(dialog, "meetingRefresh")->click();
    QTRY_VERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("A microphone and an audio output are required")));
    QVERIFY(!control<QPushButton>(dialog, "meetingStart")->isEnabled());
    QVERIFY(QFile::remove(fixture.dir.filePath(QStringLiteral("sources-empty"))));
    control<QPushButton>(dialog, "meetingRefresh")->click();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
}

void MeetingDialogTest::unavailableSavedSourceNeedsExplicitSelection()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.configPath(), "KWISPR_MEETING_MIC_SOURCE=unplugged-microphone\n"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    auto *mic = control<QComboBox>(dialog, "meetingMicrophone");
    QTRY_COMPARE(mic->count(), 1);
    QCOMPARE(mic->currentIndex(), -1);
    QVERIFY(!control<QPushButton>(dialog, "meetingStart")->isEnabled());
    mic->setCurrentIndex(0);
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
}

void MeetingDialogTest::deviceDefaultsAreAnnotatedWithoutChangingSavedSelections()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    auto sources = QJsonObject{
        {QStringLiteral("microphones"), QJsonArray{
            QJsonObject{{QStringLiteral("name"), QStringLiteral("mic.default")}, {QStringLiteral("description"), QStringLiteral("USB headset")}},
            QJsonObject{{QStringLiteral("name"), QStringLiteral("mic.saved")}, {QStringLiteral("description"), QStringLiteral("Desk mic")}}}},
        {QStringLiteral("monitors"), QJsonArray{
            QJsonObject{{QStringLiteral("name"), QStringLiteral("sink.default.monitor")}, {QStringLiteral("description"), QStringLiteral("Headphones")}},
            QJsonObject{{QStringLiteral("name"), QStringLiteral("sink.saved.monitor")}, {QStringLiteral("description"), QStringLiteral("Desk speakers")}}}},
        {QStringLiteral("default_microphone"), QStringLiteral("mic.default")},
        {QStringLiteral("default_monitor"), QStringLiteral("sink.default.monitor")},
    };
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("sources.json")), QJsonDocument(sources).toJson()));
    QVERIFY(writeFile(fixture.configPath(), "KWISPR_MEETING_MIC_SOURCE=mic.saved\nKWISPR_MEETING_MONITOR_SOURCE=sink.saved.monitor\n"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    auto *mic = control<QComboBox>(dialog, "meetingMicrophone");
    auto *monitor = control<QComboBox>(dialog, "meetingMonitor");
    QCOMPARE(mic->currentData().toString(), QStringLiteral("mic.saved"));
    QCOMPARE(monitor->currentData().toString(), QStringLiteral("sink.saved.monitor"));
    QCOMPARE(mic->itemText(mic->findData(QStringLiteral("mic.default"))), QStringLiteral("USB headset (System default)"));
    QCOMPARE(monitor->itemText(monitor->findData(QStringLiteral("sink.default.monitor"))), QStringLiteral("Headphones (System default)"));
    QCOMPARE(mic->currentText(), QStringLiteral("Desk mic"));
    QCOMPARE(monitor->currentText(), QStringLiteral("Desk speakers"));
    QCOMPARE(control<QLabel>(dialog, "meetingMicrophoneDetails")->text(), QStringLiteral("System default: USB headset"));
    QCOMPARE(control<QLabel>(dialog, "meetingMonitorDetails")->text(), QStringLiteral("System default: Headphones"));
    QVERIFY(mic->accessibleDescription().contains(QStringLiteral("System default: USB headset")));

    // A system default change updates annotations without switching the selected device.
    sources.insert(QStringLiteral("default_microphone"), QStringLiteral("mic.saved"));
    sources.insert(QStringLiteral("default_monitor"), QStringLiteral("sink.saved.monitor"));
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("sources.json")), QJsonDocument(sources).toJson()));
    control<QPushButton>(dialog, "meetingRefresh")->click();
    QTRY_COMPARE(mic->currentText(), QStringLiteral("Desk mic (System default)"));
    QCOMPARE(monitor->currentText(), QStringLiteral("Desk speakers (System default)"));
    QCOMPARE(mic->currentData().toString(), QStringLiteral("mic.saved"));
    QCOMPARE(monitor->currentData().toString(), QStringLiteral("sink.saved.monitor"));
    QCOMPARE(mic->itemText(mic->findData(QStringLiteral("mic.default"))), QStringLiteral("USB headset"));
    QCOMPARE(monitor->itemText(monitor->findData(QStringLiteral("sink.default.monitor"))), QStringLiteral("Headphones"));
    QVERIFY(!control<QLabel>(dialog, "meetingMicrophoneDetails")->isVisible());
    QVERIFY(!control<QLabel>(dialog, "meetingMonitorDetails")->isVisible());
}

void MeetingDialogTest::longSelectedDeviceNamesRemainVisible()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    const QString microphone = QStringLiteral("Focusrite Scarlett Solo USB microphone connected through the desktop docking station — analog stereo input");
    const QString output = QStringLiteral("Wireless Noise Cancelling Headphones connected over Bluetooth — High Fidelity Playback audio output");
    const auto sources = QJsonObject{
        {QStringLiteral("microphones"), QJsonArray{QJsonObject{{QStringLiteral("name"), QStringLiteral("mic.long")}, {QStringLiteral("description"), microphone}}}},
        {QStringLiteral("monitors"), QJsonArray{QJsonObject{{QStringLiteral("name"), QStringLiteral("sink.long.monitor")}, {QStringLiteral("description"), output}}}},
        {QStringLiteral("default_microphone"), QStringLiteral("mic.long")},
        {QStringLiteral("default_monitor"), QStringLiteral("sink.long.monitor")},
    };
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("sources.json")), QJsonDocument(sources).toJson()));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    for (const QSize size : {QSize(600, 660), QSize(400, 460)}) {
        dialog.resize(size);
        QTest::qWait(30);
        auto *micDetails = control<QLabel>(dialog, "meetingMicrophoneDetails");
        auto *monitorDetails = control<QLabel>(dialog, "meetingMonitorDetails");
        QVERIFY(micDetails->isVisible());
        QVERIFY(monitorDetails->isVisible());
        QCOMPARE(micDetails->text(), QStringLiteral("Selected: %1 (System default)").arg(microphone));
        QCOMPARE(monitorDetails->text(), QStringLiteral("Selected: %1 (System default)").arg(output));
        auto *scroll = control<QScrollArea>(dialog, "meetingScroll");
        QCOMPARE(scroll->horizontalScrollBar()->maximum(), 0);
        QVERIFY(micDetails->height() >= micDetails->heightForWidth(micDetails->width()));
        QVERIFY(monitorDetails->height() >= monitorDetails->heightForWidth(monitorDetails->width()));
        const QString screenshotDir = qEnvironmentVariable("KWISPR_MEETING_SCREENSHOTS");
        if (!screenshotDir.isEmpty()) {
            QDir().mkpath(screenshotDir);
            QVERIFY(dialog.grab().save(QDir(screenshotDir).filePath(QStringLiteral("meeting-long-devices-%1x%2.png").arg(size.width()).arg(size.height()))));
        }
    }
    auto *mic = control<QComboBox>(dialog, "meetingMicrophone");
    QVERIFY(mic->accessibleDescription().contains(microphone));
    QVERIFY(mic->toolTip().contains(QStringLiteral("mic.long")));
    // Opened picker exposes the same annotation through normal keyboard operation.
    mic->setFocus();
    QTest::keyClick(mic, Qt::Key_Down, Qt::AltModifier);
    QTest::keyClick(mic, Qt::Key_Escape);
    QCOMPARE(mic->currentData().toString(), QStringLiteral("mic.long"));
}

void MeetingDialogTest::meetingLanguagesAreIndependentAndPersistOnStart()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.configPath(), "KWISPR_LANGUAGE=ru\nKWISPR_WHISPER_PROMPT='Keep punctuation.'\nKWISPR_VOCABULARY='Kwispr|Codex'\n"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    auto *micLanguage = control<QComboBox>(dialog, "meetingMicrophoneLanguage");
    auto *remoteLanguage = control<QComboBox>(dialog, "meetingRemoteLanguage");
    QCOMPARE(micLanguage->currentText(), QStringLiteral("Auto"));
    QCOMPARE(remoteLanguage->currentText(), QStringLiteral("Auto"));
    micLanguage->setFocus();
    QTest::keyClick(micLanguage, Qt::Key_Down, Qt::AltModifier);
    QTRY_VERIFY(micLanguage->view()->isVisible());
    const QString screenshotDir = qEnvironmentVariable("KWISPR_MEETING_SCREENSHOTS");
    if (!screenshotDir.isEmpty()) {
        QDir().mkpath(screenshotDir);
        QVERIFY(micLanguage->view()->window()->grab().save(QDir(screenshotDir).filePath(QStringLiteral("meeting-language-picker.png"))));
    }
    QTest::keyClick(micLanguage->view(), Qt::Key_Down);
    QTest::keyClick(micLanguage->view(), Qt::Key_Return);
    QCOMPARE(micLanguage->currentData().toString(), QStringLiteral("ru"));
    remoteLanguage->setCurrentIndex(remoteLanguage->findData(QStringLiteral("en")));
    control<QPushButton>(dialog, "meetingStart")->click();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStop")->isEnabled());
    QVERIFY(!micLanguage->isEnabled());
    QVERIFY(!remoteLanguage->isEnabled());
    EnvFile env;
    QVERIFY(env.load(fixture.configPath()));
    QCOMPARE(env.value(QStringLiteral("KWISPR_MEETING_MIC_LANGUAGE")), QStringLiteral("ru"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_MEETING_REMOTE_LANGUAGE")), QStringLiteral("en"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_LANGUAGE")), QStringLiteral("ru"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_WHISPER_PROMPT")), QStringLiteral("Keep punctuation."));
    QCOMPARE(env.value(QStringLiteral("KWISPR_VOCABULARY")), QStringLiteral("Kwispr|Codex"));
    control<QPushButton>(dialog, "meetingStop")->click();
    QTRY_VERIFY(!dialog.recordingActive());
    MeetingDialog reopened(fixture.dir.path(), fixture.configPath());
    QCOMPARE(control<QComboBox>(reopened, "meetingMicrophoneLanguage")->currentData().toString(), QStringLiteral("ru"));
    QCOMPARE(control<QComboBox>(reopened, "meetingRemoteLanguage")->currentData().toString(), QStringLiteral("en"));
}

void MeetingDialogTest::retryPersistsLanguageCorrectionsWithoutChangingDeviceChoices()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(fixture.setState(QStringLiteral("failed")));
    QVERIFY(writeFile(fixture.configPath(), "KWISPR_LANGUAGE=ru\nKWISPR_MEETING_MIC_SOURCE=unplugged-microphone\nKWISPR_MEETING_MIC_LANGUAGE=ru\nKWISPR_MEETING_REMOTE_LANGUAGE=en\n"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingRetry")->isEnabled());
    auto *micLanguage = control<QComboBox>(dialog, "meetingMicrophoneLanguage");
    auto *remoteLanguage = control<QComboBox>(dialog, "meetingRemoteLanguage");
    micLanguage->setEditText(QStringLiteral(" DE "));
    remoteLanguage->setCurrentIndex(remoteLanguage->findData(QString()));
    control<QPushButton>(dialog, "meetingRetry")->click();
    QTRY_COMPARE(control<QLabel>(dialog, "meetingStatus")->text(), QStringLiteral("Transcript saved"));
    EnvFile env;
    QVERIFY(env.load(fixture.configPath()));
    QCOMPARE(env.value(QStringLiteral("KWISPR_MEETING_MIC_LANGUAGE")), QStringLiteral("de"));
    QVERIFY(env.contains(QStringLiteral("KWISPR_MEETING_REMOTE_LANGUAGE")));
    QVERIFY(env.value(QStringLiteral("KWISPR_MEETING_REMOTE_LANGUAGE")).isEmpty());
    QCOMPARE(env.value(QStringLiteral("KWISPR_MEETING_MIC_SOURCE")), QStringLiteral("unplugged-microphone"));
    QCOMPARE(env.value(QStringLiteral("KWISPR_LANGUAGE")), QStringLiteral("ru"));
    MeetingDialog reopened(fixture.dir.path(), fixture.configPath());
    QCOMPARE(control<QComboBox>(reopened, "meetingMicrophoneLanguage")->currentText(), QStringLiteral("de"));
    QCOMPARE(control<QComboBox>(reopened, "meetingRemoteLanguage")->currentText(), QStringLiteral("Auto"));
}

void MeetingDialogTest::invalidLanguageDoesNotStartRecording()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    control<QComboBox>(dialog, "meetingMicrophoneLanguage")->setEditText(QStringLiteral("not a language code"));
    control<QPushButton>(dialog, "meetingStart")->click();
    QVERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("enter a language code")));
    QVERIFY(fixture.calls(QStringLiteral("start")).isEmpty());
    QVERIFY(!dialog.recordingActive());
}

void MeetingDialogTest::openSavedFolderUsesInjectedFileManagerAndReportsErrors()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    const QString sessionDir = fixture.dir.filePath(QStringLiteral("saved meeting"));
    QVERIFY(QDir().mkpath(sessionDir));
    QVERIFY(fixture.setState(QStringLiteral("complete")));
    QString requestedFolder;
    MeetingDialog::FolderOpenCompletion pending;
    int calls = 0;
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath(), nullptr,
                         [&](const QString &folder, MeetingDialog::FolderOpenCompletion complete) {
        ++calls;
        requestedFolder = folder;
        pending = std::move(complete);
    });
    dialog.show();
    auto *open = control<QPushButton>(dialog, "meetingOpenFolder");
    QTRY_VERIFY(open->isEnabled());
    open->click();
    QCOMPARE(calls, 1);
    QCOMPARE(requestedFolder, sessionDir);
    QVERIFY(!open->isEnabled());
    QCOMPARE(open->text(), QStringLiteral("Opening folder…"));
    open->click();
    QCOMPARE(calls, 1);
    QVERIFY(pending);
    pending(false, QStringLiteral("Fixture file manager unavailable"));
    QVERIFY(open->isEnabled());
    QVERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("file manager unavailable")));
    open->click();
    QCOMPARE(calls, 2);
    pending(true, QString());
    QVERIFY(open->isEnabled());
    QVERIFY(control<QLabel>(dialog, "meetingError")->text().isEmpty());
}

void MeetingDialogTest::modelSetupIsAsynchronousAndReportsFailure()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("setup-fail")), "1"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    auto *setup = control<QPushButton>(dialog, "meetingSetup");
    setup->click();
    QVERIFY(!setup->isEnabled());
    QVERIFY(!control<QPushButton>(dialog, "meetingStart")->isEnabled());
    QTRY_VERIFY(control<QLabel>(dialog, "meetingMessage")->text().contains(QStringLiteral("Downloading fixture")));
    QTRY_VERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("Fixture download failed")));
    QTRY_VERIFY(setup->isEnabled());
    QVERIFY(QFile::remove(fixture.dir.filePath(QStringLiteral("setup-fail"))));
    setup->click();
    QTRY_VERIFY(setup->isEnabled());
    QVERIFY(control<QLabel>(dialog, "meetingError")->text().isEmpty());
}

void MeetingDialogTest::disappearingWorkerDoesNotTrapWindowInStartingState()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    auto *start = control<QPushButton>(dialog, "meetingStart");
    QTRY_VERIFY(start->isEnabled());
    QVERIFY(QFile::remove(fixture.dir.filePath(QStringLiteral("kwispr-meetings.py"))));
    start->click();
    QTRY_VERIFY(control<QLabel>(dialog, "meetingError")->text().contains(QStringLiteral("Could not launch")));
    QVERIFY(!dialog.recordingActive());
    dialog.close();
    QVERIFY(!dialog.isVisible());
    QCoreApplication::processEvents();
}

void MeetingDialogTest::pollingDoesNotOverlapOrRunWhileHidden()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(writeFile(fixture.dir.filePath(QStringLiteral("slow-status")), "1"));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTest::qWait(1100);
    QCOMPARE(fixture.calls(QStringLiteral("status")).size(), 1);
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    dialog.hide();
    const auto count = fixture.calls(QStringLiteral("status")).size();
    QTest::qWait(1200);
    QCOMPARE(fixture.calls(QStringLiteral("status")).size(), count);
}

void MeetingDialogTest::hiddenActiveMeetingKeepsPolling_data()
{
    QTest::addColumn<QString>("initialState");
    QTest::addColumn<QString>("terminalState");
    QTest::newRow("recording-failure") << QStringLiteral("recording") << QStringLiteral("failed");
    QTest::newRow("processing-completion") << QStringLiteral("processing") << QStringLiteral("complete");
}

void MeetingDialogTest::hiddenActiveMeetingKeepsPolling()
{
    QFETCH(QString, initialState);
    QFETCH(QString, terminalState);
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    QVERIFY(fixture.setState(initialState));
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    QSignalSpy stateSpy(&dialog, &MeetingDialog::meetingStateChanged);
    dialog.show();
    QTRY_VERIFY(!stateSpy.isEmpty());
    QCOMPARE(stateSpy.last().first().toString(), initialState);
    dialog.hide();
    QVERIFY(!dialog.isVisible());
    QVERIFY(fixture.setState(terminalState, QStringLiteral("Fixture terminal status")));
    QTRY_COMPARE(stateSpy.last().first().toString(), terminalState);
    QVERIFY(!dialog.recordingActive());
    const auto count = fixture.calls(QStringLiteral("status")).size();
    QVERIFY(count >= 2);
    QTest::qWait(1200);
    QCOMPARE(fixture.calls(QStringLiteral("status")).size(), count);
}

void MeetingDialogTest::narrowLayoutKeepsActionsReachable()
{
    WorkerFixture fixture;
    QVERIFY(fixture.create());
    MeetingDialog dialog(fixture.dir.path(), fixture.configPath());
    dialog.show();
    QTRY_VERIFY(control<QPushButton>(dialog, "meetingStart")->isEnabled());
    const QString screenshotDir = qEnvironmentVariable("KWISPR_MEETING_SCREENSHOTS");
    for (const QSize size : {QSize(600, 660), QSize(400, 460), QSize(460, 420)}) {
        dialog.resize(size);
        QTest::qWait(30);
        auto *scroll = control<QScrollArea>(dialog, "meetingScroll");
        if (!screenshotDir.isEmpty()) {
            QDir().mkpath(screenshotDir);
            QVERIFY(dialog.grab().save(QDir(screenshotDir).filePath(QStringLiteral("meeting-%1x%2.png").arg(size.width()).arg(size.height()))));
        }
        QCOMPARE(scroll->horizontalScrollBar()->maximum(), 0);
        for (const auto *buttonName : {"meetingStart", "meetingStop", "meetingRetry", "meetingOpenFolder"}) {
            const auto *button = control<QPushButton>(dialog, buttonName);
            QVERIFY(dialog.rect().contains(QRect(button->mapTo(&dialog, QPoint(0, 0)), button->size())));
            QVERIFY(button->height() >= 24);
        }
    }
    auto *mic = control<QComboBox>(dialog, "meetingMicrophone");
    mic->setFocus();
    QTest::keyClick(mic, Qt::Key_Down);
    QCOMPARE(mic->currentIndex(), 0);
    QTest::keyClick(mic, Qt::Key_Tab);
    QCOMPARE(dialog.focusWidget(), control<QComboBox>(dialog, "meetingMicrophoneLanguage"));
}

QTEST_MAIN(MeetingDialogTest)
#include "meeting_dialog_test.moc"
