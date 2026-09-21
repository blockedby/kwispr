#pragma once

#include <QDialog>
#include <QString>

class QComboBox;
class QLabel;
class QLineEdit;
class QProcess;
class QProgressBar;
class QPushButton;
class QSpinBox;
class QTimer;
class QJsonObject;

class MeetingDialog : public QDialog
{
    Q_OBJECT
public:
    explicit MeetingDialog(QString runtimeRoot, QString configPath, QWidget *parent = nullptr);
    ~MeetingDialog() override;
    bool recordingActive() const;

signals:
    void meetingStateChanged(const QString &state);

protected:
    void showEvent(QShowEvent *event) override;
    void hideEvent(QHideEvent *event) override;
    void closeEvent(QCloseEvent *event) override;
    void reject() override;

private:
    void loadSources();
    void refreshStatus();
    void applyStatus(const QJsonObject &status);
    void runCommand(const QStringList &arguments, const QString &pendingState);
    void startMeeting();
    void setupModels();
    bool saveChoices();
    bool captureActive() const;
    void updatePolling();
    void updateUi();

    QString m_runtimeRoot;
    QString m_configPath;
    QString m_state = QStringLiteral("loading");
    QString m_previousState;
    QString m_message;
    QString m_error;
    QString m_statusError;
    QString m_sessionDir;
    QString m_transcriptPath;
    QString m_startedAt;
    QString m_activeMic;
    QString m_activeMonitor;
    QString m_savedMic;
    QString m_savedMonitor;
    bool m_statusKnown = false;
    bool m_sourcesLoaded = false;
    bool m_commandBusy = false;
    bool m_setupBusy = false;
    bool m_commandTimedOut = false;
    bool m_statusTimedOut = false;
    bool m_sourcesTimedOut = false;
    quint64 m_generation = 0;
    quint64 m_statusGeneration = 0;

    QProcess *m_command;
    QProcess *m_statusProcess;
    QProcess *m_sourcesProcess;
    QProcess *m_setupProcess;
    QTimer *m_pollTimer;
    QTimer *m_commandTimeout;
    QTimer *m_statusTimeout;
    QTimer *m_sourcesTimeout;
    QLabel *m_statusLabel;
    QLabel *m_messageLabel;
    QLabel *m_errorLabel;
    QLabel *m_activeSourcesLabel;
    QProgressBar *m_progress;
    QLineEdit *m_titleEdit;
    QComboBox *m_micCombo;
    QComboBox *m_monitorCombo;
    QLineEdit *m_outputEdit;
    QSpinBox *m_speakersSpin;
    QPushButton *m_browseButton;
    QPushButton *m_refreshButton;
    QPushButton *m_setupButton;
    QPushButton *m_startButton;
    QPushButton *m_stopButton;
    QPushButton *m_retryButton;
    QPushButton *m_openFolderButton;
};
