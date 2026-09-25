#include "ui/AudioFilesWidget.h"
#include "ui/MeetingDialog.h"

#include <QApplication>
#include <QClipboard>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QTabWidget>
#include <QTemporaryDir>
#include <QtTest/QtTest>

namespace {
bool writeFile(const QString &path, const QByteArray &bytes)
{
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(bytes) == bytes.size();
}

class Fixture {
public:
    QTemporaryDir dir;
    bool create()
    {
        const QByteArray worker = R"PY(#!/usr/bin/env python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as file:
    file.write(json.dumps(args) + '\n')
if args[0] != 'transcribe' or '--output-dir' not in args:
    sys.exit(2)
source = pathlib.Path(args[1])
output = pathlib.Path(args[args.index('--output-dir') + 1])
print('{"event":"progress","message":"Decoding audio","completed":1,', end='', flush=True)
time.sleep(.15)
print('"total":2}', flush=True)
if source.name.startswith('slow'):
    time.sleep(1.0)
if source.name.startswith('bad'):
    print('Unsupported audio', file=sys.stderr)
    sys.exit(1)
folder = output / source.stem
folder.mkdir(parents=True, exist_ok=True)
text = 'Transcript for ' + source.name
transcript = folder / 'transcript.txt'
transcript.write_text(text)
result = {'event':'result','state':'complete','source_path':str(source),
          'text':text,'transcript_path':str(transcript),
          'transcript_json':str(folder / 'transcript.json'),'output_dir':str(folder)}
print(json.dumps(result), flush=True)
)PY";
        const QByteArray meetings = R"PY(#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
state_file = root / 'meeting-state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {'state':'idle'}
command = sys.argv[1]
if command == 'sources':
    print(json.dumps({'microphones':[{'name':'mic','description':'Microphone'}],
                      'monitors':[{'name':'monitor','description':'Call audio'}],
                      'default_microphone':'mic','default_monitor':'monitor'}))
elif command == 'status':
    print(json.dumps(state))
elif command == 'start':
    state = {'state':'recording','session_dir':str(root / 'meeting'),
             'mic_source':'mic','monitor_source':'monitor'}
    state_file.write_text(json.dumps(state))
    print(json.dumps(state))
elif command == 'stop':
    state = {'state':'queued','session_dir':str(root / 'meeting')}
    state_file.write_text(json.dumps(state))
    print(json.dumps(state))
)PY";
        const QString path = dir.filePath(QStringLiteral("kwispr-files.py"));
        const QString meetingPath = dir.filePath(QStringLiteral("kwispr-meetings.py"));
        return dir.isValid() && writeFile(path, worker) && writeFile(meetingPath, meetings)
            && QFile::setPermissions(path, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner)
            && QFile::setPermissions(meetingPath, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner);
    }
    QString source(const QString &name) const
    {
        const QString path = dir.filePath(name);
        if (!writeFile(path, "audio")) return {};
        return path;
    }
};
}

class AudioFilesWidgetTest : public QObject {
    Q_OBJECT
private slots:
    void streamsPartialProgressAndProcessesQueue();
    void hiddenMeetingWindowKeepsFileJobAndTabsIndependent();
};

void AudioFilesWidgetTest::streamsPartialProgressAndProcessesQueue()
{
    Fixture fixture;
    QVERIFY(fixture.create());
    const QString realFirst = fixture.source(QStringLiteral("slow voice.ogg"));
    const QString first = fixture.dir.filePath(QStringLiteral("voice link.ogg"));
    QVERIFY(QFile::link(realFirst, first));
    const QString second = fixture.source(QStringLiteral("bad voice.ogg"));
    const QString third = fixture.source(QStringLiteral("last voice.wav"));
    const QString output = fixture.dir.filePath(QStringLiteral("saved transcripts"));
    QString opened;
    AudioFilesWidget widget(fixture.dir.path(), fixture.dir.filePath(QStringLiteral("config.env")),
                            [&opened](const QString &folder, AudioFilesWidget::FolderOpenCompletion complete) {
                                opened = folder; complete(true, {});
                            });
    widget.resize(650, 700);
    widget.show();
    widget.findChild<QLineEdit *>(QStringLiteral("audioFilesOutput"))->setText(output);
    widget.enqueueFiles({first, second, third});
    QVERIFY(widget.hasPendingWork());
    auto *status = widget.findChild<QLabel *>(QStringLiteral("audioFilesStatus"));
    auto *progress = widget.findChild<QProgressBar *>(QStringLiteral("audioFilesProgress"));
    QTRY_COMPARE(status->text(), QStringLiteral("Decoding audio"));
    QCOMPARE(progress->maximum(), 2);
    auto *jobs = widget.findChild<QListWidget *>(QStringLiteral("audioFilesJobs"));
    QTRY_COMPARE(jobs->item(2)->text(), QStringLiteral("Complete · last voice.wav"));
    QTRY_VERIFY(!widget.hasPendingWork());
    QCOMPARE(jobs->count(), 3);
    QVERIFY(jobs->item(0)->text().startsWith(QStringLiteral("Complete")));
    QVERIFY(jobs->item(1)->text().startsWith(QStringLiteral("Failed")));
    QCOMPARE(widget.findChild<QLabel *>(QStringLiteral("audioFilesError"))->text(),
             QStringLiteral("Could not transcribe bad voice.ogg: Unsupported audio"));
    jobs->setCurrentRow(0);
    QCOMPARE(widget.findChild<QPlainTextEdit *>(QStringLiteral("audioFilesTranscript"))->toPlainText(),
             QStringLiteral("Transcript for slow voice.ogg"));
    widget.findChild<QPushButton *>(QStringLiteral("audioFilesCopy"))->click();
    QCOMPARE(QApplication::clipboard()->text(), QStringLiteral("Transcript for slow voice.ogg"));
    widget.findChild<QPushButton *>(QStringLiteral("audioFilesOpenFolder"))->click();
    QCOMPARE(opened, output + QStringLiteral("/slow voice"));
    const QString screenshot = qEnvironmentVariable("AUDIO_FILES_SCREENSHOT");
    if (!screenshot.isEmpty()) QVERIFY(widget.grab().save(screenshot));
    QFile calls(fixture.dir.filePath(QStringLiteral("calls.jsonl")));
    QVERIFY(calls.open(QIODevice::ReadOnly));
    const auto lines = calls.readAll().trimmed().split('\n');
    QCOMPARE(lines.size(), 3);
    for (const auto &line : lines) {
        const auto args = QJsonDocument::fromJson(line).array();
        QCOMPARE(args.at(0).toString(), QStringLiteral("transcribe"));
        QCOMPARE(args.at(2).toString(), QStringLiteral("--output-dir"));
        QCOMPARE(args.at(3).toString(), output);
    }
    QCOMPARE(QJsonDocument::fromJson(lines.first()).array().at(1).toString(), realFirst);
}

void AudioFilesWidgetTest::hiddenMeetingWindowKeepsFileJobAndTabsIndependent()
{
    Fixture fixture;
    QVERIFY(fixture.create());
    const QString source = fixture.source(QStringLiteral("slow hidden.opus"));
    MeetingDialog dialog(fixture.dir.path(), fixture.dir.filePath(QStringLiteral("config.env")), nullptr,
                         [](const QString &, MeetingDialog::FolderOpenCompletion complete) { complete(true, {}); });
    dialog.show();
    QTRY_VERIFY(dialog.findChild<QPushButton *>(QStringLiteral("meetingStart"))->isEnabled());
    auto *tabs = dialog.findChild<QTabWidget *>(QStringLiteral("kwisprTabs"));
    QVERIFY(tabs);
    QCOMPARE(tabs->tabText(0), QStringLiteral("Meetings"));
    QCOMPARE(tabs->tabText(1), QStringLiteral("Audio files"));
    dialog.showAudioFiles();
    QCOMPARE(tabs->currentIndex(), 1);
    auto *files = dialog.findChild<AudioFilesWidget *>();
    QVERIFY(files);
    files->findChild<QLineEdit *>(QStringLiteral("audioFilesOutput"))->setText(fixture.dir.filePath(QStringLiteral("saved")));
    files->enqueueFiles({source});
    QVERIFY(dialog.audioFilesPending());
    tabs->setCurrentIndex(0);
    dialog.findChild<QPushButton *>(QStringLiteral("meetingStart"))->click();
    QTRY_VERIFY(dialog.recordingActive());
    QVERIFY(dialog.audioFilesPending());
    QTRY_VERIFY(dialog.findChild<QPushButton *>(QStringLiteral("meetingStop"))->isEnabled());
    dialog.findChild<QPushButton *>(QStringLiteral("meetingStop"))->click();
    QTRY_VERIFY(!dialog.recordingActive());
    dialog.showAudioFiles();
    dialog.close();
    QVERIFY(!dialog.isVisible());
    QTRY_VERIFY(!dialog.audioFilesPending());
    dialog.show();
    QCOMPARE(files->findChild<QPlainTextEdit *>(QStringLiteral("audioFilesTranscript"))->toPlainText(),
             QStringLiteral("Transcript for slow hidden.opus"));
    QVERIFY(dialog.findChild<QPushButton *>(QStringLiteral("meetingStart")));
    QVERIFY(!dialog.recordingActive());
    const QString screenshot = qEnvironmentVariable("AUDIO_FILES_TABS_SCREENSHOT");
    if (!screenshot.isEmpty()) QVERIFY(dialog.grab().save(screenshot));
}

QTEST_MAIN(AudioFilesWidgetTest)
#include "audio_files_widget_test.moc"
