#include "ui/AudioFilesWidget.h"

#include <QApplication>
#include <QClipboard>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProcessEnvironment>
#include <QProgressBar>
#include <QPointer>
#include <QPushButton>
#include <QStandardPaths>
#include <QVBoxLayout>

namespace {
constexpr int resultRole = Qt::UserRole;
QString defaultOutputRoot()
{
    QString documents = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    if (documents.isEmpty()) documents = QDir::homePath() + QStringLiteral("/Documents");
    return documents + QStringLiteral("/Kwispr/Transcriptions");
}
}

AudioFilesWidget::AudioFilesWidget(QString runtimeRoot, QString configPath, FolderOpener folderOpener, QWidget *parent)
    : QWidget(parent), m_runtimeRoot(std::move(runtimeRoot)), m_configPath(std::move(configPath)),
      m_folderOpener(std::move(folderOpener)), m_process(new QProcess(this))
{
    auto *layout = new QVBoxLayout(this);
    auto *intro = new QLabel(tr("Transcribe Telegram voice messages and other audio files with local Whisper. Files are processed one at a time and saved automatically."), this);
    intro->setWordWrap(true);
    layout->addWidget(intro);
    auto *outputRow = new QHBoxLayout;
    m_outputEdit = new QLineEdit(defaultOutputRoot(), this);
    m_outputEdit->setObjectName(QStringLiteral("audioFilesOutput"));
    m_browseButton = new QPushButton(tr("Choose folder…"), this);
    m_browseButton->setObjectName(QStringLiteral("audioFilesBrowse"));
    outputRow->addWidget(m_outputEdit, 1);
    outputRow->addWidget(m_browseButton);
    auto *outputLabel = new QLabel(tr("Save &folder"), this);
    outputLabel->setBuddy(m_outputEdit);
    layout->addWidget(outputLabel);
    layout->addLayout(outputRow);
    m_addButton = new QPushButton(tr("Add audio files…"), this);
    m_addButton->setObjectName(QStringLiteral("audioFilesAdd"));
    layout->addWidget(m_addButton);
    m_status = new QLabel(tr("Choose audio files to transcribe."), this);
    m_status->setObjectName(QStringLiteral("audioFilesStatus"));
    m_status->setTextFormat(Qt::PlainText);
    m_status->setWordWrap(true);
    layout->addWidget(m_status);
    m_progress = new QProgressBar(this);
    m_progress->setObjectName(QStringLiteral("audioFilesProgress"));
    m_progress->setRange(0, 0);
    m_progress->hide();
    layout->addWidget(m_progress);
    m_errorLabel = new QLabel(this);
    m_errorLabel->setObjectName(QStringLiteral("audioFilesError"));
    m_errorLabel->setTextFormat(Qt::PlainText);
    m_errorLabel->setWordWrap(true);
    m_errorLabel->setTextInteractionFlags(Qt::TextSelectableByMouse | Qt::TextSelectableByKeyboard);
    m_errorLabel->hide();
    layout->addWidget(m_errorLabel);
    m_jobs = new QListWidget(this);
    m_jobs->setObjectName(QStringLiteral("audioFilesJobs"));
    layout->addWidget(m_jobs, 1);
    auto *transcriptLabel = new QLabel(tr("Transcript"), this);
    layout->addWidget(transcriptLabel);
    m_transcript = new QPlainTextEdit(this);
    m_transcript->setObjectName(QStringLiteral("audioFilesTranscript"));
    m_transcript->setReadOnly(true);
    m_transcript->setPlaceholderText(tr("Select a completed file to read its transcript."));
    layout->addWidget(m_transcript, 2);
    auto *actions = new QHBoxLayout;
    m_copyButton = new QPushButton(tr("Copy text"), this);
    m_copyButton->setObjectName(QStringLiteral("audioFilesCopy"));
    m_openButton = new QPushButton(tr("Open saved folder"), this);
    m_openButton->setObjectName(QStringLiteral("audioFilesOpenFolder"));
    actions->addWidget(m_copyButton);
    actions->addWidget(m_openButton);
    layout->addLayout(actions);
    for (auto *button : {m_addButton, m_browseButton, m_copyButton, m_openButton}) button->setAutoDefault(false);

    connect(m_browseButton, &QPushButton::clicked, this, [this] {
        const QString path = QFileDialog::getExistingDirectory(this, tr("Save transcripts in"), m_outputEdit->text());
        if (!path.isEmpty()) m_outputEdit->setText(path);
    });
    connect(m_addButton, &QPushButton::clicked, this, [this] {
        enqueueFiles(QFileDialog::getOpenFileNames(this, tr("Choose audio files"), QDir::homePath(),
                                                   tr("Audio files (*.ogg *.oga *.opus *.mp3 *.m4a *.mp4 *.wav *.flac *.webm *.aac *.wma);;All files (*)")));
    });
    connect(m_jobs, &QListWidget::currentRowChanged, this, &AudioFilesWidget::showSelectedResult);
    connect(m_copyButton, &QPushButton::clicked, this, [this] { QApplication::clipboard()->setText(m_transcript->toPlainText()); });
    connect(m_openButton, &QPushButton::clicked, this, [this] {
        const auto result = m_jobs->currentItem()->data(resultRole).toJsonObject();
        const QString folder = result.value(QStringLiteral("output_dir")).toString();
        m_folderOpenBusy = true;
        updateActions();
        QPointer<AudioFilesWidget> widget(this);
        m_folderOpener(folder, [widget](bool ok, const QString &error) {
            if (!widget) return;
            widget->m_folderOpenBusy = false;
            if (!ok) { widget->m_errorLabel->setText(error); widget->m_errorLabel->show(); }
            widget->updateActions();
        });
    });
    connect(m_outputEdit, &QLineEdit::textChanged, this, &AudioFilesWidget::updateActions);
    connect(m_process, &QProcess::readyReadStandardOutput, this, &AudioFilesWidget::readOutput);
    connect(m_process, &QProcess::finished, this, [this](int code, QProcess::ExitStatus status) {
        readOutput();
        finishJob(code, status == QProcess::NormalExit);
    });
    connect(m_process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) finishJob(-1, false);
    });
    updateActions();
}

AudioFilesWidget::~AudioFilesWidget()
{
    disconnect(m_process, nullptr, this, nullptr);
    if (m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        m_process->waitForFinished(3000);
    }
}

bool AudioFilesWidget::hasPendingWork() const { return m_running || !m_queue.isEmpty(); }

void AudioFilesWidget::enqueueFiles(const QStringList &paths)
{
    const QString outputRoot = m_outputEdit->text().trimmed();
    if (paths.isEmpty()) return;
    if (outputRoot.isEmpty()) {
        m_errorLabel->setText(tr("Choose a save folder before adding files."));
        m_errorLabel->show();
        return;
    }
    int added = 0;
    for (const QString &path : paths) {
        const QFileInfo source(path);
        if (!source.isFile() || !source.isReadable()) {
            m_errorLabel->setText(tr("Cannot read audio file: %1").arg(path));
            m_errorLabel->show();
            continue;
        }
        auto *item = new QListWidgetItem(tr("Queued · %1").arg(source.fileName()), m_jobs);
        item->setToolTip(source.canonicalFilePath());
        m_queue.enqueue({source.canonicalFilePath(), outputRoot, m_jobs->row(item)});
        ++added;
    }
    if (added && !m_running) startNext();
}

void AudioFilesWidget::startNext()
{
    if (m_running || m_queue.isEmpty()) { updateActions(); return; }
    m_current = m_queue.dequeue();
    m_running = true;
    m_hadResult = false;
    m_result = {};
    m_error.clear();
    m_stdoutBuffer.clear();
    auto *item = m_jobs->item(m_current.row);
    item->setText(tr("Transcribing · %1").arg(QFileInfo(m_current.source).fileName()));
    m_jobs->setCurrentItem(item);
    m_status->setText(tr("Transcribing %1…").arg(QFileInfo(m_current.source).fileName()));
    m_progress->setRange(0, 0);
    m_progress->show();
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert(QStringLiteral("KWISPR_CONFIG_FILE"), m_configPath);
    m_process->setProcessEnvironment(env);
    m_process->start(QStringLiteral("python3"), {m_runtimeRoot + QStringLiteral("/kwispr-files.py"),
        QStringLiteral("transcribe"), m_current.source, QStringLiteral("--output-dir"), m_current.outputRoot});
    updateActions();
}

void AudioFilesWidget::readOutput()
{
    m_stdoutBuffer += m_process->readAllStandardOutput();
    for (qsizetype end; (end = m_stdoutBuffer.indexOf('\n')) >= 0;) {
        const QByteArray line = m_stdoutBuffer.left(end);
        m_stdoutBuffer.remove(0, end + 1);
        consumeLine(line);
    }
    if (m_stdoutBuffer.size() > 32 * 1024 * 1024) {
        m_error = tr("The transcription worker sent an oversized response.");
        m_process->kill();
    }
}

void AudioFilesWidget::consumeLine(const QByteArray &line)
{
    QJsonParseError parseError;
    const auto doc = QJsonDocument::fromJson(line.trimmed(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) return;
    const auto object = doc.object();
    const QString event = object.value(QStringLiteral("event")).toString();
    if (event == QStringLiteral("progress")) {
        const QString message = object.value(QStringLiteral("message")).toString();
        if (!message.isEmpty()) m_status->setText(message);
        const int total = object.value(QStringLiteral("total")).toInt();
        const int completed = object.value(QStringLiteral("completed")).toInt();
        if (total > 0) { m_progress->setRange(0, total); m_progress->setValue(qBound(0, completed, total)); }
    } else if (event == QStringLiteral("result") && object.value(QStringLiteral("state")).toString() == QStringLiteral("complete")) {
        m_hadResult = true;
        m_result = object;
    }
}

void AudioFilesWidget::finishJob(int exitCode, bool normalExit)
{
    if (!m_running) return;
    if (!m_stdoutBuffer.trimmed().isEmpty()) consumeLine(m_stdoutBuffer);
    m_stdoutBuffer.clear();
    auto *item = m_jobs->item(m_current.row);
    const QString stderrText = QString::fromUtf8(m_process->readAllStandardError()).trimmed().right(4000);
    const bool success = normalExit && exitCode == 0 && m_hadResult
        && m_result.value(QStringLiteral("source_path")).toString() == m_current.source
        && m_result.value(QStringLiteral("text")).isString()
        && QFileInfo(m_result.value(QStringLiteral("transcript_path")).toString()).isFile()
        && QFileInfo(m_result.value(QStringLiteral("output_dir")).toString()).isDir();
    if (success) {
        item->setText(tr("Complete · %1").arg(QFileInfo(m_current.source).fileName()));
        item->setData(resultRole, m_result);
        m_status->setText(tr("Transcript saved for %1.").arg(QFileInfo(m_current.source).fileName()));
    } else {
        item->setText(tr("Failed · %1").arg(QFileInfo(m_current.source).fileName()));
        const QString reason = !m_error.isEmpty() ? m_error : !stderrText.isEmpty() ? stderrText
            : exitCode == 0 ? tr("The worker did not return a complete transcript.") : m_process->errorString();
        m_errorLabel->setText(tr("Could not transcribe %1: %2").arg(QFileInfo(m_current.source).fileName(), reason));
        m_errorLabel->show();
        m_status->setText(tr("Transcription failed. Continuing with queued files."));
    }
    m_running = false;
    m_progress->hide();
    showSelectedResult();
    // Defer starting the next process until the current QProcess has finished
    // delivering its finished/error signals.
    QMetaObject::invokeMethod(this, &AudioFilesWidget::startNext, Qt::QueuedConnection);
}

void AudioFilesWidget::showSelectedResult()
{
    const auto *item = m_jobs->currentItem();
    const auto result = item ? item->data(resultRole).toJsonObject() : QJsonObject();
    m_transcript->setPlainText(result.value(QStringLiteral("text")).toString());
    updateActions();
}

void AudioFilesWidget::updateActions()
{
    const auto *item = m_jobs->currentItem();
    const auto result = item ? item->data(resultRole).toJsonObject() : QJsonObject();
    m_addButton->setEnabled(!m_outputEdit->text().trimmed().isEmpty());
    m_copyButton->setEnabled(!result.value(QStringLiteral("text")).toString().isEmpty());
    m_openButton->setEnabled(!m_folderOpenBusy && !result.value(QStringLiteral("output_dir")).toString().isEmpty());
}
