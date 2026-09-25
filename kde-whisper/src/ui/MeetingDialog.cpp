#include "ui/MeetingDialog.h"

#include "config/EnvFile.h"

#include <QCloseEvent>
#include <QComboBox>
#include <QCoreApplication>
#include <QDateTime>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusPendingCallWatcher>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHideEvent>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QProcess>
#include <QPointer>
#include <QProgressBar>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QShowEvent>
#include <QSpinBox>
#include <QStandardPaths>
#include <QStyle>
#include <QStyleOptionComboBox>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>

namespace {
QString processError(const QByteArray &output, const QString &fallback)
{
    const auto doc = QJsonDocument::fromJson(output);
    const QString message = doc.object().value(QStringLiteral("message")).toString();
    if (!message.isEmpty()) {
        return message;
    }
    const QString text = QString::fromUtf8(output).trimmed();
    return text.isEmpty() ? fallback : text.right(4000);
}

QLabel *wrapLabel(const QString &text, QWidget *parent)
{
    auto *label = new QLabel(text, parent);
    label->setWordWrap(true);
    label->setTextFormat(Qt::PlainText);
    label->setMinimumWidth(0);
    return label;
}

void fillSources(QComboBox *combo, const QJsonArray &sources, const QString &selected, const QString &systemDefault)
{
    combo->clear();
    for (const auto &value : sources) {
        const auto source = value.toObject();
        const QString name = source.value(QStringLiteral("name")).toString();
        if (name.isEmpty()) {
            continue;
        }
        const QString description = source.value(QStringLiteral("description")).toString(name);
        const QString label = name == systemDefault
            ? QCoreApplication::translate("MeetingDialog", "%1 (System default)").arg(description)
            : description;
        combo->addItem(label, name);
        combo->setItemData(combo->count() - 1, name, Qt::ToolTipRole);
        combo->setItemData(combo->count() - 1, description, Qt::UserRole + 1);
    }
    const int selectedIndex = combo->findData(selected);
    // An unplugged preferred source must not silently select a different device.
    combo->setCurrentIndex(selected.isEmpty() ? (combo->count() ? 0 : -1) : selectedIndex);
}

QComboBox *languageCombo(QWidget *parent, const QString &objectName)
{
    auto *combo = new QComboBox(parent);
    combo->setObjectName(objectName);
    combo->setEditable(true);
    combo->setInsertPolicy(QComboBox::NoInsert);
    combo->addItem(QCoreApplication::translate("MeetingDialog", "Auto"), QString());
    combo->addItem(QCoreApplication::translate("MeetingDialog", "Russian (ru)"), QStringLiteral("ru"));
    combo->addItem(QCoreApplication::translate("MeetingDialog", "English (en)"), QStringLiteral("en"));
    combo->lineEdit()->setMaxLength(32);
    combo->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    return combo;
}

QString languageCode(const QComboBox *combo)
{
    const int index = combo->currentIndex();
    if (index >= 0 && combo->currentText() == combo->itemText(index)) {
        return combo->itemData(index).toString();
    }
    const QString text = combo->currentText().trimmed().toLower();
    return text == QStringLiteral("auto") ? QString() : text;
}

void selectLanguage(QComboBox *combo, const QString &code)
{
    const QString normalized = code.trimmed().toLower();
    const int index = combo->findData(normalized == QStringLiteral("auto") ? QString() : normalized);
    if (index >= 0) {
        combo->setCurrentIndex(index);
    } else {
        combo->setEditText(normalized);
    }
}

void showFolderInFileManager(QObject *owner, const QString &folder, MeetingDialog::FolderOpenCompletion complete)
{
    const QString path = QFileInfo(folder).absoluteFilePath();
    auto message = QDBusMessage::createMethodCall(QStringLiteral("org.freedesktop.FileManager1"),
                                                 QStringLiteral("/org/freedesktop/FileManager1"),
                                                 QStringLiteral("org.freedesktop.FileManager1"),
                                                 QStringLiteral("ShowFolders"));
    message.setArguments({QStringList{QUrl::fromLocalFile(path).toString(QUrl::FullyEncoded)}, QString()});
    auto *watcher = new QDBusPendingCallWatcher(QDBusConnection::sessionBus().asyncCall(message, 5000), owner);
    QObject::connect(watcher, &QDBusPendingCallWatcher::finished, owner,
                     [path, complete = std::move(complete)](QDBusPendingCallWatcher *call) {
        const QDBusMessage reply = call->reply();
        call->deleteLater();
        if (reply.type() != QDBusMessage::ErrorMessage) {
            complete(true, QString());
            return;
        }
        // A generic file URL launcher can select an editor registered for
        // inode/directory. Fall back only to known file-manager executables.
        for (const auto &name : {QStringLiteral("dolphin"), QStringLiteral("nautilus"), QStringLiteral("thunar"),
                                 QStringLiteral("nemo"), QStringLiteral("pcmanfm-qt"), QStringLiteral("pcmanfm")}) {
            const QString program = QStandardPaths::findExecutable(name);
            if (!program.isEmpty() && QProcess::startDetached(program, {path})) {
                complete(true, QString());
                return;
            }
        }
        complete(false, QCoreApplication::translate("MeetingDialog", "Could not open a file manager for %1: %2")
                            .arg(path, reply.errorMessage()));
    });
}
}

MeetingDialog::MeetingDialog(QString runtimeRoot, QString configPath, QWidget *parent, FolderOpener folderOpener)
    : QDialog(parent)
    , m_runtimeRoot(std::move(runtimeRoot))
    , m_configPath(std::move(configPath))
    , m_folderOpener(std::move(folderOpener))
    , m_command(new QProcess(this))
    , m_statusProcess(new QProcess(this))
    , m_sourcesProcess(new QProcess(this))
    , m_setupProcess(new QProcess(this))
    , m_pollTimer(new QTimer(this))
    , m_commandTimeout(new QTimer(this))
    , m_statusTimeout(new QTimer(this))
    , m_sourcesTimeout(new QTimer(this))
{
    if (!m_folderOpener) {
        m_folderOpener = [this](const QString &folder, FolderOpenCompletion complete) {
            showFolderInFileManager(this, folder, std::move(complete));
        };
    }
    setWindowTitle(tr("Meetings"));
    setWindowFlag(Qt::WindowMinimizeButtonHint);
    setModal(false);
    resize(600, 660);
    setMinimumSize(380, 380);

    auto *layout = new QVBoxLayout(this);
    m_statusLabel = wrapLabel(tr("Checking meeting status…"), this);
    m_statusLabel->setObjectName(QStringLiteral("meetingStatus"));
    QFont statusFont = m_statusLabel->font();
    statusFont.setBold(true);
    m_statusLabel->setFont(statusFont);
    layout->addWidget(m_statusLabel);
    m_progress = new QProgressBar(this);
    m_progress->setRange(0, 0);
    m_progress->setTextVisible(false);
    m_progress->setMaximumHeight(6);
    layout->addWidget(m_progress);

    auto *scroll = new QScrollArea(this);
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scroll->setObjectName(QStringLiteral("meetingScroll"));
    auto *body = new QWidget(scroll);
    auto *bodyLayout = new QVBoxLayout(body);
    bodyLayout->setContentsMargins(0, 0, 0, 0);
    m_messageLabel = wrapLabel(QString(), body);
    m_messageLabel->setObjectName(QStringLiteral("meetingMessage"));
    bodyLayout->addWidget(m_messageLabel);
    m_backgroundStatusLabel = wrapLabel(QString(), body);
    m_backgroundStatusLabel->setObjectName(QStringLiteral("meetingBackgroundStatus"));
    bodyLayout->addWidget(m_backgroundStatusLabel);
    m_errorLabel = wrapLabel(QString(), body);
    m_errorLabel->setObjectName(QStringLiteral("meetingError"));
    m_errorLabel->setTextInteractionFlags(Qt::TextSelectableByMouse | Qt::TextSelectableByKeyboard);
    bodyLayout->addWidget(m_errorLabel);
    m_activeSourcesLabel = wrapLabel(QString(), body);
    m_activeSourcesLabel->setObjectName(QStringLiteral("meetingActiveSources"));
    bodyLayout->addWidget(m_activeSourcesLabel);

    auto *form = new QFormLayout;
    form->setRowWrapPolicy(QFormLayout::WrapAllRows);
    form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
    m_titleEdit = new QLineEdit(body);
    m_titleEdit->setObjectName(QStringLiteral("meetingTitle"));
    m_titleEdit->setMaxLength(160);
    form->addRow(tr("Meeting &title (optional)"), m_titleEdit);
    m_micCombo = new QComboBox(body);
    m_micCombo->setObjectName(QStringLiteral("meetingMicrophone"));
    m_micCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_micCombo->setMinimumContentsLength(12);
    m_micCombo->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    m_micCombo->setPlaceholderText(tr("Select a microphone"));
    form->addRow(tr("&Microphone — You"), m_micCombo);
    m_micDetailsLabel = wrapLabel(QString(), body);
    m_micDetailsLabel->setObjectName(QStringLiteral("meetingMicrophoneDetails"));
    form->addRow(m_micDetailsLabel);
    m_micLanguageCombo = languageCombo(body, QStringLiteral("meetingMicrophoneLanguage"));
    form->addRow(tr("Your speech &language"), m_micLanguageCombo);
    m_monitorCombo = new QComboBox(body);
    m_monitorCombo->setObjectName(QStringLiteral("meetingMonitor"));
    m_monitorCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_monitorCombo->setMinimumContentsLength(12);
    m_monitorCombo->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    m_monitorCombo->setPlaceholderText(tr("Select the call's audio output"));
    form->addRow(tr("Call audio &output"), m_monitorCombo);
    m_monitorDetailsLabel = wrapLabel(QString(), body);
    m_monitorDetailsLabel->setObjectName(QStringLiteral("meetingMonitorDetails"));
    form->addRow(m_monitorDetailsLabel);
    m_remoteLanguageCombo = languageCombo(body, QStringLiteral("meetingRemoteLanguage"));
    form->addRow(tr("Other speakers' la&nguage"), m_remoteLanguageCombo);
    form->addRow(wrapLabel(tr("Use Auto, or type a language code such as de. These choices do not change dictation."), body));
    for (auto *detail : {m_micDetailsLabel, m_monitorDetailsLabel}) {
        auto policy = detail->sizePolicy();
        policy.setHorizontalPolicy(QSizePolicy::Ignored);
        detail->setSizePolicy(policy);
        detail->setTextInteractionFlags(Qt::TextSelectableByMouse);
    }
    form->addRow(wrapLabel(tr("All sound played on this output is recorded. Use headphones to keep the call audio out of your microphone."), body));
    m_refreshButton = new QPushButton(tr("Refresh audio devices"), body);
    m_refreshButton->setObjectName(QStringLiteral("meetingRefresh"));
    form->addRow(m_refreshButton);
    m_speakersSpin = new QSpinBox(body);
    m_speakersSpin->setObjectName(QStringLiteral("meetingSpeakers"));
    m_speakersSpin->setRange(0, 16);
    m_speakersSpin->setSpecialValueText(tr("Auto"));
    form->addRow(tr("Other &speakers"), m_speakersSpin);
    form->addRow(wrapLabel(tr("Other voices are labeled Speaker 1, Speaker 2, etc. within each meeting. Names are not inferred."), body));
    m_outputEdit = new QLineEdit(body);
    m_outputEdit->setObjectName(QStringLiteral("meetingOutput"));
    m_browseButton = new QPushButton(tr("Choose folder…"), body);
    m_browseButton->setObjectName(QStringLiteral("meetingBrowse"));
    auto *outputRow = new QHBoxLayout;
    outputRow->addWidget(m_outputEdit, 1);
    outputRow->addWidget(m_browseButton);
    auto *outputLabel = new QLabel(tr("Save &folder"), body);
    outputLabel->setBuddy(m_outputEdit);
    form->addRow(outputLabel, outputRow);
    form->addRow(wrapLabel(tr("After Stop, the recording is transcribed into files in this folder."), body));
    m_setupButton = new QPushButton(tr("Set up meeting models"), body);
    m_setupButton->setObjectName(QStringLiteral("meetingSetup"));
    form->addRow(m_setupButton);
    form->addRow(wrapLabel(tr("First use requires downloading the meeting models. This can take several minutes."), body));
    bodyLayout->addLayout(form);
    bodyLayout->addStretch();
    scroll->setWidget(body);
    layout->addWidget(scroll, 1);

    auto *recordActions = new QHBoxLayout;
    m_startButton = new QPushButton(tr("Start recording"), this);
    m_startButton->setObjectName(QStringLiteral("meetingStart"));
    m_stopButton = new QPushButton(tr("Stop && transcribe"), this);
    m_stopButton->setObjectName(QStringLiteral("meetingStop"));
    recordActions->addWidget(m_startButton);
    recordActions->addWidget(m_stopButton);
    layout->addLayout(recordActions);
    auto *fileActions = new QHBoxLayout;
    m_retryButton = new QPushButton(tr("Retry transcription"), this);
    m_retryButton->setObjectName(QStringLiteral("meetingRetry"));
    m_openFolderButton = new QPushButton(tr("Open saved folder"), this);
    m_openFolderButton->setObjectName(QStringLiteral("meetingOpenFolder"));
    fileActions->addWidget(m_retryButton);
    fileActions->addWidget(m_openFolderButton);
    layout->addLayout(fileActions);
    for (auto *button : findChildren<QPushButton *>()) {
        button->setAutoDefault(false);
        button->setMinimumHeight(28);
    }

    EnvFile env;
    env.load(m_configPath);
    QString documents = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    if (documents.isEmpty()) {
        documents = QDir::homePath() + QStringLiteral("/Documents");
    }
    m_outputEdit->setText(env.value(QStringLiteral("KWISPR_MEETING_OUTPUT_DIR"), documents + QStringLiteral("/Kwispr/Meetings")));
    m_savedMic = env.value(QStringLiteral("KWISPR_MEETING_MIC_SOURCE"));
    m_savedMonitor = env.value(QStringLiteral("KWISPR_MEETING_MONITOR_SOURCE"));
    m_speakersSpin->setValue(env.value(QStringLiteral("KWISPR_MEETING_SPEAKERS"), QStringLiteral("0")).toInt());
    selectLanguage(m_micLanguageCombo, env.value(QStringLiteral("KWISPR_MEETING_MIC_LANGUAGE")));
    selectLanguage(m_remoteLanguageCombo, env.value(QStringLiteral("KWISPR_MEETING_REMOTE_LANGUAGE")));

    connect(m_startButton, &QPushButton::clicked, this, &MeetingDialog::startMeeting);
    connect(m_stopButton, &QPushButton::clicked, this, [this] { runCommand({QStringLiteral("stop")}, QStringLiteral("stopping")); });
    connect(m_retryButton, &QPushButton::clicked, this, [this] {
        if (saveChoices(true)) {
            runCommand({QStringLiteral("process"), m_sessionDir,
                        QStringLiteral("--speakers"), QString::number(m_speakersSpin->value())}, QStringLiteral("processing"));
        }
    });
    connect(m_refreshButton, &QPushButton::clicked, this, &MeetingDialog::loadSources);
    connect(m_setupButton, &QPushButton::clicked, this, &MeetingDialog::setupModels);
    connect(m_micCombo, &QComboBox::currentIndexChanged, this, [this] { updateUi(); });
    connect(m_monitorCombo, &QComboBox::currentIndexChanged, this, [this] { updateUi(); });
    m_micCombo->installEventFilter(this);
    m_monitorCombo->installEventFilter(this);
    connect(m_outputEdit, &QLineEdit::textChanged, this, [this] { updateUi(); });
    connect(m_browseButton, &QPushButton::clicked, this, [this] {
        const QString path = QFileDialog::getExistingDirectory(this, tr("Save meetings in"), m_outputEdit->text());
        if (!path.isEmpty()) {
            m_outputEdit->setText(path);
        }
    });
    connect(m_openFolderButton, &QPushButton::clicked, this, &MeetingDialog::openSavedFolder);

    m_pollTimer->setInterval(1000);
    connect(m_pollTimer, &QTimer::timeout, this, &MeetingDialog::refreshStatus);
    for (auto *timer : {m_commandTimeout, m_statusTimeout, m_sourcesTimeout}) {
        timer->setInterval(15000);
        timer->setSingleShot(true);
    }
    connect(m_commandTimeout, &QTimer::timeout, this, [this] { m_commandTimedOut = true; m_command->kill(); });
    connect(m_statusTimeout, &QTimer::timeout, this, [this] { m_statusTimedOut = true; m_statusProcess->kill(); });
    connect(m_sourcesTimeout, &QTimer::timeout, this, [this] { m_sourcesTimedOut = true; m_sourcesProcess->kill(); });

    connect(m_command, &QProcess::finished, this, [this](int code, QProcess::ExitStatus exitStatus) {
        m_commandTimeout->stop();
        m_commandBusy = false;
        const auto output = m_command->readAllStandardOutput();
        const auto errorOutput = m_command->readAllStandardError();
        if (code == 0 && exitStatus == QProcess::NormalExit && !m_commandTimedOut) {
            const auto doc = QJsonDocument::fromJson(output);
            if (doc.isObject() && doc.object().contains(QStringLiteral("state"))) {
                applyStatus(doc.object());
            } else {
                m_error = tr("The meeting worker returned an invalid response. Checking the recording status…");
            }
        } else {
            m_error = m_commandTimedOut ? tr("The meeting command timed out. Checking whether it took effect…")
                                       : processError(errorOutput, tr("The meeting command failed."));
        }
        updateUi();
        refreshStatus();
    });
    connect(m_command, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            m_commandTimeout->stop();
            m_commandBusy = false;
            m_state = m_previousState;
            emit meetingStateChanged(m_state);
            m_error = tr("Could not launch the meeting worker: %1").arg(m_command->errorString());
            updateUi();
            refreshStatus();
        }
    });
    connect(m_statusProcess, &QProcess::finished, this, [this](int code, QProcess::ExitStatus exitStatus) {
        m_statusTimeout->stop();
        const auto output = m_statusProcess->readAllStandardOutput();
        const auto errorOutput = m_statusProcess->readAllStandardError();
        if (m_statusGeneration != m_generation) {
            return;
        }
        const auto doc = QJsonDocument::fromJson(output);
        if (code == 0 && exitStatus == QProcess::NormalExit && doc.isObject() && doc.object().contains(QStringLiteral("state")) && !m_statusTimedOut) {
            m_statusKnown = true;
            m_statusError.clear();
            applyStatus(doc.object());
        } else {
            m_statusKnown = false;
            m_statusError = m_statusTimedOut ? tr("Meeting status timed out.") : processError(errorOutput, tr("Could not read meeting status."));
            updateUi();
        }
    });
    connect(m_statusProcess, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            m_statusTimeout->stop();
            m_statusKnown = false;
            m_statusError = tr("Could not launch the meeting worker: %1").arg(m_statusProcess->errorString());
            updateUi();
        }
    });
    connect(m_sourcesProcess, &QProcess::finished, this, [this](int code, QProcess::ExitStatus exitStatus) {
        m_sourcesTimeout->stop();
        const auto doc = QJsonDocument::fromJson(m_sourcesProcess->readAllStandardOutput());
        const auto errorOutput = m_sourcesProcess->readAllStandardError();
        if (code == 0 && exitStatus == QProcess::NormalExit && doc.isObject() && doc.object().value(QStringLiteral("microphones")).isArray() && doc.object().value(QStringLiteral("monitors")).isArray() && !m_sourcesTimedOut) {
            const auto sources = doc.object();
            const QString mic = m_micCombo->currentData().toString();
            const QString monitor = m_monitorCombo->currentData().toString();
            m_defaultMic = sources.value(QStringLiteral("default_microphone")).toString();
            m_defaultMonitor = sources.value(QStringLiteral("default_monitor")).toString();
            fillSources(m_micCombo, sources.value(QStringLiteral("microphones")).toArray(), mic.isEmpty() ? (m_savedMic.isEmpty() ? m_defaultMic : m_savedMic) : mic, m_defaultMic);
            fillSources(m_monitorCombo, sources.value(QStringLiteral("monitors")).toArray(), monitor.isEmpty() ? (m_savedMonitor.isEmpty() ? m_defaultMonitor : m_savedMonitor) : monitor, m_defaultMonitor);
            m_sourcesLoaded = true;
            if (!m_micCombo->count() || !m_monitorCombo->count()) {
                m_error = tr("A microphone and an audio output are required. Connect them, then refresh audio devices.");
            }
        } else {
            m_sourcesLoaded = false;
            m_error = m_sourcesTimedOut ? tr("Audio device discovery timed out.") : processError(errorOutput, tr("Could not list audio devices."));
        }
        updateUi();
    });
    connect(m_sourcesProcess, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            m_sourcesTimeout->stop();
            m_sourcesLoaded = false;
            m_error = tr("Could not launch audio device discovery: %1").arg(m_sourcesProcess->errorString());
            updateUi();
        }
    });
    m_setupProcess->setProcessChannelMode(QProcess::MergedChannels);
    connect(m_setupProcess, &QProcess::readyReadStandardOutput, this, [this] {
        const QString progress = QString::fromUtf8(m_setupProcess->readAllStandardOutput()).trimmed();
        if (!progress.isEmpty()) {
            m_message = progress.right(2000);
            updateUi();
        }
    });
    connect(m_setupProcess, &QProcess::finished, this, [this](int code, QProcess::ExitStatus exitStatus) {
        m_setupBusy = false;
        if (code == 0 && exitStatus == QProcess::NormalExit) {
            m_message = tr("Meeting models are ready.");
        } else {
            m_error = tr("Meeting model setup failed. %1").arg(m_message);
            m_message.clear();
        }
        updateUi();
        refreshStatus();
    });
    connect(m_setupProcess, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            m_setupBusy = false;
            m_error = tr("Could not launch meeting model setup: %1").arg(m_setupProcess->errorString());
            updateUi();
        }
    });
    updateUi();
}

MeetingDialog::~MeetingDialog()
{
    for (auto *timer : {m_pollTimer, m_commandTimeout, m_statusTimeout, m_sourcesTimeout}) {
        timer->stop();
    }
    // QProcess destruction can emit finished while QWidget deletes its children,
    // after our QString members are gone. Prevent callbacks into that partial object.
    for (auto *process : {m_command, m_statusProcess, m_sourcesProcess, m_setupProcess}) {
        QObject::disconnect(process, nullptr, this, nullptr);
    }
}

void MeetingDialog::showEvent(QShowEvent *event)
{
    QDialog::showEvent(event);
    m_pollTimer->start();
    refreshStatus();
    if (!m_sourcesLoaded) {
        loadSources();
    }
}

void MeetingDialog::hideEvent(QHideEvent *event)
{
    QDialog::hideEvent(event);
    updatePolling();
}

void MeetingDialog::closeEvent(QCloseEvent *event)
{
    if (captureActive()) {
        m_error = tr("Stop recording before closing this window. You can minimize it while the call continues.");
        updateUi();
        event->ignore();
        return;
    }
    QDialog::closeEvent(event);
}

void MeetingDialog::reject()
{
    if (captureActive()) {
        m_error = tr("Stop recording before closing this window. You can minimize it while the call continues.");
        updateUi();
        return;
    }
    QDialog::reject();
}

bool MeetingDialog::eventFilter(QObject *watched, QEvent *event)
{
    if ((watched == m_micCombo || watched == m_monitorCombo) && event->type() == QEvent::Resize) {
        updateDeviceDetails();
    }
    return QDialog::eventFilter(watched, event);
}

void MeetingDialog::loadSources()
{
    if (m_sourcesProcess->state() != QProcess::NotRunning || captureActive()) {
        return;
    }
    m_error.clear();
    m_sourcesTimedOut = false;
    m_sourcesProcess->start(m_runtimeRoot + QStringLiteral("/kwispr-meetings.py"), {QStringLiteral("sources"), QStringLiteral("--json")});
    m_sourcesTimeout->start();
    updateUi();
}

void MeetingDialog::refreshStatus()
{
    if (m_statusProcess->state() != QProcess::NotRunning || m_commandBusy || m_setupBusy) {
        return;
    }
    m_statusGeneration = m_generation;
    m_statusTimedOut = false;
    m_statusProcess->start(m_runtimeRoot + QStringLiteral("/kwispr-meetings.py"), {QStringLiteral("status"), QStringLiteral("--json")});
    m_statusTimeout->start();
}

void MeetingDialog::applyStatus(const QJsonObject &status)
{
    const QString state = status.value(QStringLiteral("state")).toString();
    const QStringList validStates = {QStringLiteral("idle"), QStringLiteral("starting"), QStringLiteral("recording"), QStringLiteral("stopping"), QStringLiteral("queued"), QStringLiteral("processing"), QStringLiteral("complete"), QStringLiteral("failed")};
    if (!validStates.contains(state)) {
        m_statusKnown = false;
        m_statusError = tr("The meeting worker returned an unknown state: %1").arg(state);
        updateUi();
        return;
    }

    const bool hasRecording = status.contains(QStringLiteral("recording"));
    const bool hasTranscription = status.contains(QStringLiteral("transcription"));
    const bool hasQueue = status.contains(QStringLiteral("queue"));
    const bool hasQueueLength = status.contains(QStringLiteral("queue_length"));
    const bool hasAnyQueueField = hasRecording || hasTranscription || hasQueue || hasQueueLength;
    const bool hasAllQueueFields = hasRecording && hasTranscription && hasQueue && hasQueueLength;
    if (hasAnyQueueField && !hasAllQueueFields) {
        m_statusKnown = false;
        m_statusError = tr("The meeting worker returned an incomplete recording queue status.");
        updateUi();
        return;
    }

    bool recordingActive = false;
    QString recordingSessionDir;
    bool transcriptionActive = false;
    QString transcriptionSessionDir;
    QString backgroundStatus;
    QStringList queuedSessionDirs;
    int queueLength = 0;
    if (hasAllQueueFields) {
        const QJsonValue recordingValue = status.value(QStringLiteral("recording"));
        if (!recordingValue.isNull() && !recordingValue.isObject()) {
            m_statusKnown = false;
            m_statusError = tr("The meeting worker returned an invalid recording status.");
            updateUi();
            return;
        }
        if (recordingValue.isObject()) {
            const QJsonObject recording = recordingValue.toObject();
            const QString recordingState = recording.value(QStringLiteral("state")).toString();
            recordingSessionDir = recording.value(QStringLiteral("session_dir")).toString();
            if (!QStringList{QStringLiteral("starting"), QStringLiteral("recording"), QStringLiteral("stopping")}.contains(recordingState)
                || recordingSessionDir.isEmpty() || state != recordingState) {
                m_statusKnown = false;
                m_statusError = tr("The meeting worker returned an inconsistent recording status.");
                updateUi();
                return;
            }
            recordingActive = true;
        } else if (state == QStringLiteral("starting") || state == QStringLiteral("recording") || state == QStringLiteral("stopping")) {
            m_statusKnown = false;
            m_statusError = tr("The meeting worker omitted the active recording status.");
            updateUi();
            return;
        }

        const QJsonValue transcriptionValue = status.value(QStringLiteral("transcription"));
        if (!transcriptionValue.isNull() && !transcriptionValue.isObject()) {
            m_statusKnown = false;
            m_statusError = tr("The meeting worker returned an invalid transcription status.");
            updateUi();
            return;
        }
        if (transcriptionValue.isObject()) {
            const QJsonObject transcription = transcriptionValue.toObject();
            transcriptionSessionDir = transcription.value(QStringLiteral("session_dir")).toString();
            if (transcription.value(QStringLiteral("state")).toString() != QStringLiteral("processing") || transcriptionSessionDir.isEmpty()) {
                m_statusKnown = false;
                m_statusError = tr("The meeting worker returned an inconsistent transcription status.");
                updateUi();
                return;
            }
            transcriptionActive = true;
            const QString title = transcription.value(QStringLiteral("title")).toString().trimmed();
            const QString message = transcription.value(QStringLiteral("message")).toString().trimmed();
            QStringList details;
            if (!message.isEmpty()) {
                details.append(message);
            }
            const QJsonValue progressValue = transcription.value(QStringLiteral("progress"));
            if (progressValue.isObject()) {
                const QJsonObject progress = progressValue.toObject();
                QStringList progressParts;
                const QString stage = progress.value(QStringLiteral("stage")).toString().trimmed();
                const QString track = progress.value(QStringLiteral("track")).toString().trimmed();
                if (!stage.isEmpty()) progressParts.append(stage);
                if (!track.isEmpty()) progressParts.append(track);
                const QJsonValue indexValue = progress.value(QStringLiteral("index"));
                const QJsonValue totalValue = progress.value(QStringLiteral("total"));
                const int progressIndex = indexValue.toInt(-1);
                const int progressTotal = totalValue.toInt(-1);
                if (progressIndex >= 0 && progressTotal > 0) {
                    progressParts.append(tr("%1/%2").arg(progressIndex).arg(progressTotal));
                }
                if (!progressParts.isEmpty()) {
                    details.append(progressParts.join(QStringLiteral(" · ")));
                }
            }
            backgroundStatus = tr("Transcribing %1")
                .arg(title.isEmpty() ? tr("meeting") : title);
            if (!details.isEmpty()) {
                backgroundStatus += QStringLiteral(" — ") + details.join(QStringLiteral(" · "));
            }
        }

        const QJsonValue queueValue = status.value(QStringLiteral("queue"));
        const QJsonValue queueLengthValue = status.value(QStringLiteral("queue_length"));
        if (!queueValue.isArray() || !queueLengthValue.isDouble()) {
            m_statusKnown = false;
            m_statusError = tr("The meeting worker returned an invalid queue.");
            updateUi();
            return;
        }
        const double reportedQueueLength = queueLengthValue.toDouble(-1.0);
        const QJsonArray queue = queueValue.toArray();
        if (reportedQueueLength < 0.0 || reportedQueueLength != static_cast<double>(queue.size())) {
            m_statusKnown = false;
            m_statusError = tr("The meeting worker returned an inconsistent queue length.");
            updateUi();
            return;
        }
        queueLength = queue.size();
        for (const QJsonValue &value : queue) {
            if (!value.isObject()) {
                m_statusKnown = false;
                m_statusError = tr("The meeting worker returned an invalid queued meeting.");
                updateUi();
                return;
            }
            const QJsonObject queued = value.toObject();
            const QString sessionDir = queued.value(QStringLiteral("session_dir")).toString();
            if (queued.value(QStringLiteral("state")).toString() != QStringLiteral("queued") || sessionDir.isEmpty()) {
                m_statusKnown = false;
                m_statusError = tr("The meeting worker returned an invalid queued meeting.");
                updateUi();
                return;
            }
            queuedSessionDirs.append(sessionDir);
        }
        if (queueLength > 0) {
            const QString waiting = queueLength == 1
                ? tr("1 meeting waiting in queue")
                : tr("%1 meetings waiting in queue").arg(queueLength);
            backgroundStatus = backgroundStatus.isEmpty() ? waiting : backgroundStatus + QLatin1Char('\n') + waiting;
        }
    } else if (state == QStringLiteral("processing")) {
        // Older workers expose a single processing state and do not support
        // concurrent capture or a durable queue.
        backgroundStatus.clear();
    }

    const bool changed = m_state != state;
    m_state = state;
    m_hasQueueContract = hasAllQueueFields;
    m_recordingActive = recordingActive;
    m_recordingSessionDir = recordingSessionDir;
    m_transcriptionActive = transcriptionActive;
    m_transcriptionSessionDir = transcriptionSessionDir;
    m_queuedSessionDirs = queuedSessionDirs;
    m_queueLength = queueLength;
    m_backgroundStatus = backgroundStatus;
    m_statusKnown = true;
    m_statusError.clear();
    m_message = status.value(QStringLiteral("message")).toString();
    m_sessionDir = status.value(QStringLiteral("session_dir")).toString();
    if (!m_sessionDir.isEmpty() && m_speakersSessionDir != m_sessionDir) {
        m_speakersSessionDir = m_sessionDir;
        const QJsonValue savedSpeakers = status.value(QStringLiteral("speakers"));
        if (savedSpeakers.isDouble()) {
            const int count = savedSpeakers.toInt(-1);
            if (count >= m_speakersSpin->minimum() && count <= m_speakersSpin->maximum()) {
                m_speakersSpin->setValue(count);
            }
        }
    }
    m_transcriptPath = status.value(QStringLiteral("transcript_path")).toString();
    m_startedAt = status.value(QStringLiteral("started_at")).toString();
    m_activeMic = status.value(QStringLiteral("mic_source")).toString();
    m_activeMonitor = status.value(QStringLiteral("monitor_source")).toString();
    if (changed) {
        emit meetingStateChanged(m_state);
    }
    updateUi();
}

void MeetingDialog::runCommand(const QStringList &arguments, const QString &pendingState)
{
    if (m_commandBusy || m_setupBusy) {
        return;
    }
    ++m_generation;
    m_commandBusy = true;
    m_commandTimedOut = false;
    m_error.clear();
    m_previousState = m_state;
    m_state = pendingState;
    emit meetingStateChanged(m_state);
    m_command->start(m_runtimeRoot + QStringLiteral("/kwispr-meetings.py"), arguments);
    m_commandTimeout->start();
    updateUi();
}

void MeetingDialog::startMeeting()
{
    if (!m_startButton->isEnabled() || !saveChoices()) {
        return;
    }
    m_activeMic = m_micCombo->currentData().toString();
    m_activeMonitor = m_monitorCombo->currentData().toString();
    runCommand({QStringLiteral("start"), QStringLiteral("--mic"), m_activeMic,
                QStringLiteral("--monitor"), m_activeMonitor,
                QStringLiteral("--output-dir"), m_outputEdit->text().trimmed(),
                QStringLiteral("--speakers"), QString::number(m_speakersSpin->value()),
                QStringLiteral("--title"), m_titleEdit->text().trimmed()}, QStringLiteral("starting"));
}

void MeetingDialog::setupModels()
{
    const bool processing = m_hasQueueContract ? m_transcriptionActive : m_state == QStringLiteral("processing");
    if (m_setupBusy || m_commandBusy || captureActive() || processing) {
        return;
    }
    ++m_generation;
    m_setupBusy = true;
    m_error.clear();
    m_message = tr("Downloading and preparing meeting models…");
    m_setupProcess->start(m_runtimeRoot + QStringLiteral("/kwispr-meetings-setup.py"), {});
    updateUi();
}

void MeetingDialog::openSavedFolder()
{
    if (m_folderOpenBusy || m_sessionDir.isEmpty() || !QFileInfo(m_sessionDir).isDir()) {
        return;
    }
    m_folderOpenBusy = true;
    m_error.clear();
    updateUi();
    QPointer<MeetingDialog> dialog(this);
    m_folderOpener(m_sessionDir, [dialog](bool success, const QString &error) {
        if (!dialog) {
            return;
        }
        dialog->m_folderOpenBusy = false;
        if (!success) {
            dialog->m_error = error;
        }
        dialog->updateUi();
    });
}

bool MeetingDialog::saveChoices(bool languagesOnly)
{
    const QString micLanguage = languageCode(m_micLanguageCombo);
    const QString remoteLanguage = languageCode(m_remoteLanguageCombo);
    static const QRegularExpression languagePattern(QStringLiteral("^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$"));
    for (auto *combo : {m_micLanguageCombo, m_remoteLanguageCombo}) {
        const QString code = languageCode(combo);
        if (!code.isEmpty() && !languagePattern.match(code).hasMatch()) {
            m_error = tr("Choose Auto or enter a language code, such as ru, en, or de.");
            combo->setFocus();
            updateUi();
            return false;
        }
    }
    EnvFile env;
    if (QFileInfo::exists(m_configPath) && !env.load(m_configPath)) {
        m_error = tr("Could not read settings: %1").arg(env.errorString());
        updateUi();
        return false;
    }
    if (!languagesOnly) {
        env.setValue(QStringLiteral("KWISPR_MEETING_OUTPUT_DIR"), m_outputEdit->text().trimmed());
        env.setValue(QStringLiteral("KWISPR_MEETING_MIC_SOURCE"), m_micCombo->currentData().toString());
        env.setValue(QStringLiteral("KWISPR_MEETING_MONITOR_SOURCE"), m_monitorCombo->currentData().toString());
        env.setValue(QStringLiteral("KWISPR_MEETING_SPEAKERS"), QString::number(m_speakersSpin->value()));
    }
    env.setValue(QStringLiteral("KWISPR_MEETING_MIC_LANGUAGE"), micLanguage);
    env.setValue(QStringLiteral("KWISPR_MEETING_REMOTE_LANGUAGE"), remoteLanguage);
    if (!QDir().mkpath(QFileInfo(m_configPath).absolutePath()) || !env.save(m_configPath)) {
        m_error = tr("Could not save meeting settings: %1").arg(env.errorString());
        updateUi();
        return false;
    }
    return true;
}

bool MeetingDialog::captureActive() const
{
    if (m_hasQueueContract) {
        // A Start command is asynchronous: retain the pending-start capture
        // state until its response updates the nested recording object.
        return m_recordingActive || m_state == QStringLiteral("starting");
    }
    return m_state == QStringLiteral("recording") || m_state == QStringLiteral("starting") || m_state == QStringLiteral("stopping");
}

bool MeetingDialog::recordingActive() const
{
    return captureActive();
}

void MeetingDialog::updatePolling()
{
    const bool processing = m_hasQueueContract ? m_transcriptionActive : m_state == QStringLiteral("processing");
    const bool active = captureActive() || processing || m_queueLength > 0 || m_commandBusy || m_setupBusy;
    if (isVisible() || active) {
        if (!m_pollTimer->isActive()) {
            m_pollTimer->start();
        }
    } else {
        m_pollTimer->stop();
    }
}

void MeetingDialog::updateUi()
{
    updatePolling();
    updateDeviceDetails();
    const bool capturing = captureActive();
    const bool processing = m_hasQueueContract ? m_transcriptionActive : m_state == QStringLiteral("processing");
    // New workers permit a fresh capture while an earlier session is being
    // transcribed. Legacy workers keep their previous single-session behavior.
    const bool editable = !capturing && (m_hasQueueContract || m_state != QStringLiteral("processing"))
        && !m_commandBusy && !m_setupBusy;
    for (QWidget *field : QList<QWidget *>{m_titleEdit, m_micCombo, m_monitorCombo, m_micLanguageCombo, m_remoteLanguageCombo, m_outputEdit, m_speakersSpin, m_browseButton}) {
        field->setEnabled(editable);
    }
    m_refreshButton->setEnabled(editable && m_sourcesProcess->state() == QProcess::NotRunning);
    m_setupButton->setEnabled(editable && !processing && QFileInfo::exists(m_runtimeRoot + QStringLiteral("/kwispr-meetings-setup.py")));
    m_startButton->setEnabled(editable && m_statusKnown && m_sourcesLoaded && m_micCombo->currentIndex() >= 0 && m_monitorCombo->currentIndex() >= 0 && !m_outputEdit->text().trimmed().isEmpty());
    // A failed status poll must not hide the Stop action for a last-known live
    // capture. The previous valid recording object is retained on parse errors.
    m_stopButton->setEnabled(capturing && m_state == QStringLiteral("recording") && !m_commandBusy);
    const bool retryTargetBusy = capturing || (!m_sessionDir.isEmpty()
        && (m_sessionDir == m_recordingSessionDir || m_sessionDir == m_transcriptionSessionDir || m_queuedSessionDirs.contains(m_sessionDir)));
    m_retryButton->setEnabled(m_statusKnown && m_state == QStringLiteral("failed") && !m_sessionDir.isEmpty()
                               && !retryTargetBusy && !m_commandBusy && !m_setupBusy);
    m_openFolderButton->setEnabled(!m_folderOpenBusy && !m_sessionDir.isEmpty() && QFileInfo(m_sessionDir).isDir());
    m_openFolderButton->setText(m_folderOpenBusy ? tr("Opening folder…") : tr("Open saved folder"));
    m_progress->setVisible(m_commandBusy || m_setupBusy || processing || (m_state == QStringLiteral("loading") && m_statusError.isEmpty()) || m_state == QStringLiteral("starting") || m_state == QStringLiteral("stopping"));

    QString status = tr("Ready to record");
    if (m_setupBusy) status = tr("Preparing meeting models…");
    else if (m_state == QStringLiteral("loading")) status = m_statusError.isEmpty() ? tr("Checking meeting status…") : tr("Meeting status unavailable");
    else if (capturing && m_state == QStringLiteral("starting")) status = tr("Starting recording…");
    else if (capturing && m_state == QStringLiteral("recording")) {
        status = tr("● Recording");
        const auto start = QDateTime::fromString(m_startedAt, Qt::ISODate);
        if (start.isValid()) {
            const auto seconds = qMax<qint64>(0, start.secsTo(QDateTime::currentDateTimeUtc()));
            status += QStringLiteral(" · %1:%2").arg(seconds / 60).arg(seconds % 60, 2, 10, QLatin1Char('0'));
        }
    }
    else if (capturing && m_state == QStringLiteral("stopping")) status = tr("Finishing recording…");
    else if (m_state == QStringLiteral("queued")) status = tr("Waiting in transcription queue");
    else if (processing) status = tr("Transcribing meeting…");
    else if (m_state == QStringLiteral("complete")) status = tr("Transcript saved");
    else if (m_state == QStringLiteral("failed")) status = tr("Meeting needs attention");
    m_statusLabel->setText(status);
    setWindowTitle(capturing ? tr("Recording — Meetings") : tr("Meetings"));
    m_messageLabel->setText(m_message);
    m_messageLabel->setVisible(!m_message.isEmpty());
    m_backgroundStatusLabel->setText(m_backgroundStatus);
    m_backgroundStatusLabel->setVisible(!m_backgroundStatus.isEmpty());
    const QString error = m_error + (!m_error.isEmpty() && !m_statusError.isEmpty() ? QStringLiteral("\n") : QString()) + m_statusError;
    m_errorLabel->setText(error);
    m_errorLabel->setVisible(!error.isEmpty());
    const QString activeSources = tr("Microphone: %1\nCall audio: %2").arg(m_activeMic, m_activeMonitor);
    m_activeSourcesLabel->setText(activeSources);
    m_activeSourcesLabel->setVisible(capturing && (!m_activeMic.isEmpty() || !m_activeMonitor.isEmpty()));
}

void MeetingDialog::updateDeviceDetails()
{
    const auto update = [this](QComboBox *combo, QLabel *details, const QString &systemDefault) {
        const QString selected = combo->currentText();
        const QString source = combo->currentData().toString();
        const int defaultIndex = combo->findData(systemDefault);
        const QString defaultDescription = defaultIndex >= 0 ? combo->itemData(defaultIndex, Qt::UserRole + 1).toString() : systemDefault;
        QStyleOptionComboBox option;
        option.initFrom(combo);
        option.currentText = selected;
        option.frame = combo->hasFrame();
        const QRect textRect = combo->style()->subControlRect(QStyle::CC_ComboBox, &option, QStyle::SC_ComboBoxEditField, combo);
        QStringList lines;
        if (!selected.isEmpty() && combo->fontMetrics().horizontalAdvance(selected) > textRect.width()) {
            lines.append(tr("Selected: %1").arg(selected));
        }
        if (m_sourcesLoaded && !systemDefault.isEmpty() && source != systemDefault) {
            lines.append(tr("System default: %1").arg(defaultDescription));
        }
        details->setText(lines.join(QLatin1Char('\n')));
        // QFormLayout can otherwise compress wrapped device names when the
        // form grows taller than its scroll viewport.
        details->setMinimumHeight(lines.isEmpty() ? 0 : details->heightForWidth(combo->width()));
        details->setVisible(!lines.isEmpty());
        combo->setToolTip(selected.isEmpty() ? QString() : selected + QLatin1Char('\n') + source);
        combo->setAccessibleDescription(systemDefault.isEmpty() ? selected : tr("Selected: %1. System default: %2").arg(selected, defaultDescription));
    };
    update(m_micCombo, m_micDetailsLabel, m_defaultMic);
    update(m_monitorCombo, m_monitorDetailsLabel, m_defaultMonitor);
}
