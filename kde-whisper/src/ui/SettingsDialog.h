#pragma once

#include "config/EnvFile.h"
#include "config/KwisprSettings.h"
#include "models/ModelCatalog.h"

#include <QDialog>
#include <QElapsedTimer>
#include <QHash>
#include <QKeySequence>
#include <QSet>

class GlobalShortcutManager;
class ModelManager;
class QCheckBox;
class QCloseEvent;
class QComboBox;
class QDialogButtonBox;
class QDoubleSpinBox;
class QFormLayout;
class QGroupBox;
class QLabel;
class QKeySequenceEdit;
class QLineEdit;
class QPlainTextEdit;
class QProgressBar;
class QPushButton;
class QSpinBox;
class QWidget;

class SettingsDialog : public QDialog {
    Q_OBJECT
public:
    explicit SettingsDialog(const KwisprSettings &settings,
                            const ModelCatalog &catalog,
                            const QStringList &installedModelIds,
                            EnvFile *env = nullptr,
                            ModelManager *modelManager = nullptr,
                            bool localRuntimeInstalled = true,
                            GlobalShortcutManager *globalShortcut = nullptr,
                            QWidget *parent = nullptr);

    bool save();
    QString lastError() const;
    KwisprSettings currentSettings() const;

public slots:
    void accept() override;
    void reject() override;

signals:
    void settingsSaved(const KwisprSettings &settings);

private slots:
    void applyBackendPreset(const QString &backendLabel);
    void startModelDownload();
    void confirmAndDeleteModel();
    void modelOperationStarted(const QString &operation, const QString &modelId);
    void modelOperationFinished(const QString &operation,
                                const QString &modelId,
                                bool success,
                                const QString &stdoutText,
                                const QString &stderrText);
    void modelDownloadStatus(const QString &modelId,
                             const QString &status,
                             qint64 bytesDone,
                             qint64 bytesTotal);
    void modelDownloadProgress(const QString &modelId,
                               qint64 bytesDone,
                               qint64 bytesTotal);

protected:
    void closeEvent(QCloseEvent *event) override;

private:
    struct BackendDraft {
        QString apiUrl;
        QString apiKey;
        QString model;
        QString language;
        QString prompt;
    };

    void buildUi();
    void loadFromSettings(const KwisprSettings &settings);
    void setGlobalShortcutClean(const QKeySequence &shortcut, bool justApplied);
    void populateModels(const QString &selectedModelId);
    void populateLanguageChoices(const QString &languageCode);
    QString selectedLanguageCode() const;
    static QString formatEta(qint64 seconds);
    void updateBackendVisibility();
    void updateModelControls();
    void updateVadControls();
    void showBusyCloseStatus();
    bool selectedModelIsCatalogModel() const;
    void setBackendRowVisible(QWidget *field, QLabel *label, bool visible);
    void saveActiveBackendDraft();
    void loadBackendDraft(const QString &backendLabel);
    QString selectedModelId() const;
    QString selectedModelName() const;
    QString operationError(const QString &stdoutText, const QString &stderrText) const;
    KwisprSettings settingsFromWidgets() const;
    QString backendLabelForSettings(const KwisprSettings &settings) const;

    ModelCatalog m_catalog;
    QSet<QString> m_installedModelIds;
    EnvFile *m_env = nullptr;
    ModelManager *m_modelManager = nullptr;
    GlobalShortcutManager *m_globalShortcut = nullptr;
    KwisprSettings m_settings;
    QString m_lastError;
    QString m_activeBackend;
    QHash<QString, BackendDraft> m_backendDrafts;
    QKeySequence m_globalShortcutBaseline;
    bool m_globalShortcutDirty = false;
    bool m_modelOperationBusy = false;
    bool m_localRuntimeInstalled = false;
    QString m_activeModelOperation;
    QString m_activeModelId;
    QElapsedTimer m_downloadElapsed;

    QFormLayout *m_backendForm = nullptr;
    QComboBox *m_backendCombo = nullptr;
    QLabel *m_localSttSectionLabel = nullptr;
    QLineEdit *m_apiUrlEdit = nullptr;
    QLabel *m_apiUrlLabel = nullptr;
    QLabel *m_resolvedUrlLabel = nullptr;
    QLabel *m_resolvedUrlFieldLabel = nullptr;
    QLineEdit *m_localSttHostEdit = nullptr;
    QLabel *m_localSttHostLabel = nullptr;
    QSpinBox *m_localSttPortSpin = nullptr;
    QLabel *m_localSttPortLabel = nullptr;
    bool m_localSttPortNeedsCorrection = false;
    QCheckBox *m_localSttAllowLanCheck = nullptr;
    QLineEdit *m_apiKeyEdit = nullptr;
    QLabel *m_apiKeyLabel = nullptr;
    QLineEdit *m_modelEdit = nullptr;
    QLabel *m_modelLabel = nullptr;
    QWidget *m_localModelRow = nullptr;
    QLabel *m_localModelLabel = nullptr;
    QComboBox *m_localModelCombo = nullptr;
    QPushButton *m_downloadButton = nullptr;
    QPushButton *m_deleteButton = nullptr;
    QProgressBar *m_modelBusyIndicator = nullptr;
    QLabel *m_modelStatusLabel = nullptr;
    QLabel *m_modelDownloadPercentLabel = nullptr;
    QLabel *m_modelDownloadEtaLabel = nullptr;
    QComboBox *m_languageCombo = nullptr;
    QLabel *m_languageLabel = nullptr;
    QPlainTextEdit *m_promptEdit = nullptr;
    QLabel *m_promptLabel = nullptr;
    QGroupBox *m_vadGroup = nullptr;
    QKeySequenceEdit *m_globalShortcutEdit = nullptr;
    QLabel *m_globalShortcutStatusLabel = nullptr;
    QCheckBox *m_autopasteCheck = nullptr;
    QComboBox *m_pasteHotkeyCombo = nullptr;
    QDoubleSpinBox *m_autopasteDelaySpin = nullptr;
    QCheckBox *m_vadEnabledCheck = nullptr;
    QComboBox *m_vadProviderCombo = nullptr;
    QLineEdit *m_vadModelPathEdit = nullptr;
    QLabel *m_vadModelPathLabel = nullptr;
    QDoubleSpinBox *m_vadThresholdSpin = nullptr;
    QLineEdit *m_vadFrameMsEdit = nullptr;
    QDialogButtonBox *m_buttons = nullptr;
};
