#include "ui/SettingsDialog.h"

#include "models/ModelManager.h"
#include "ui/GlobalShortcut.h"

#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFormLayout>
#include <QFont>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QGridLayout>
#include <QHostAddress>
#include <QLabel>
#include <QKeySequenceEdit>
#include <QLineEdit>
#include <QLocale>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSignalBlocker>
#include <QSpinBox>
#include <QUrl>
#include <QVBoxLayout>
#include <QtMath>

namespace {
constexpr const char *LocalUrl = "http://127.0.0.1:19650/v1/audio/transcriptions";
constexpr const char *OpenAiUrl = "https://api.openai.com/v1/audio/transcriptions";
constexpr const char *OpenRouterUrl = "https://openrouter.ai/api/v1/chat/completions";

QString backendValueForLabel(const QString &label)
{
    if (label == QLatin1String("OpenRouter")) {
        return QStringLiteral("openrouter-chat");
    }
    return QStringLiteral("openai-transcriptions");
}

QLabel *formLabel(const QString &text, const QString &objectName, QWidget *parent)
{
    auto *label = new QLabel(text, parent);
    label->setObjectName(objectName);
    return label;
}

QString languageLabel(const QString &code)
{
    const QLocale locale(code);
    QString name = QLocale::languageToString(locale.language());
    if (locale.language() == QLocale::C || name.isEmpty()) {
        name = code.toUpper();
    }
    return QStringLiteral("%1 (%2)").arg(name, code);
}

QString baseLanguage(const QString &language)
{
    const qsizetype separator = language.indexOf(QLatin1Char('-'));
    return separator < 0 ? language : language.left(separator);
}

std::optional<QString> effectiveCatalogLanguage(const LocalModel &model, const QString &language)
{
    const QString requested = language.trimmed();
    if (requested.isEmpty() || requested.compare(QStringLiteral("auto"), Qt::CaseInsensitive) == 0) {
        if (model.supportsLanguageDetection) {
            return QString();
        }
        for (const QString &candidate : model.languages) {
            if (baseLanguage(candidate).compare(QStringLiteral("en"), Qt::CaseInsensitive) == 0) {
                return candidate;
            }
        }
        if (!model.languages.isEmpty()) {
            return model.languages.constFirst();
        }
        return std::nullopt;
    }

    for (const QString &candidate : model.languages) {
        if (candidate.compare(requested, Qt::CaseInsensitive) == 0
            || baseLanguage(candidate).compare(baseLanguage(requested), Qt::CaseInsensitive) == 0) {
            return candidate;
        }
    }
    return std::nullopt;
}
}

SettingsDialog::SettingsDialog(const KwisprSettings &settings,
                               const ModelCatalog &catalog,
                               const QStringList &installedModelIds,
                               EnvFile *env,
                               ModelManager *modelManager,
                               bool localRuntimeInstalled,
                               GlobalShortcutManager *globalShortcut,
                               QWidget *parent)
    : QDialog(parent)
    , m_catalog(catalog)
    , m_installedModelIds(installedModelIds.begin(), installedModelIds.end())
    , m_env(env)
    , m_modelManager(modelManager)
    , m_globalShortcut(globalShortcut)
    , m_settings(settings)
    , m_localRuntimeInstalled(localRuntimeInstalled)
{
    buildUi();
    populateModels(QString());
    loadFromSettings(settings);

    if (m_modelManager) {
        connect(m_modelManager, &ModelManager::operationStarted,
                this, &SettingsDialog::modelOperationStarted);
        connect(m_modelManager, &ModelManager::operationFinished,
                this, &SettingsDialog::modelOperationFinished);
        connect(m_modelManager, &ModelManager::downloadStatus,
                this, &SettingsDialog::modelDownloadStatus);
        connect(m_modelManager, &ModelManager::downloadProgress,
                this, &SettingsDialog::modelDownloadProgress);
        m_modelOperationBusy = m_modelManager->isBusy();
        if (m_modelOperationBusy) {
            m_modelStatusLabel->setText(QStringLiteral("A model operation is already running. Wait for it to finish."));
        }
    }
    updateModelControls();
}

QString SettingsDialog::lastError() const
{
    return m_lastError;
}

KwisprSettings SettingsDialog::currentSettings() const
{
    return settingsFromWidgets();
}

bool SettingsDialog::save()
{
    m_lastError.clear();
    if (m_modelOperationBusy) {
        m_lastError = QStringLiteral("Wait for the current model operation to finish before saving settings.");
        m_modelStatusLabel->setText(m_lastError);
        return false;
    }

    const bool local = m_backendCombo->currentText() == QLatin1String("Local STT");
    if (local && (!m_catalog.isValid || m_catalog.models.isEmpty())) {
        m_lastError = QStringLiteral("Cannot save Local STT settings because the local model catalog is invalid or empty.");
        if (!m_catalog.error.trimmed().isEmpty()) {
            m_lastError += QStringLiteral(" ") + m_catalog.error.trimmed();
        }
        m_modelStatusLabel->setText(m_lastError);
        return false;
    }
    if (local && selectedModelId().trimmed().isEmpty()) {
        m_lastError = QStringLiteral("Select a local model before saving Local STT settings.");
        m_modelStatusLabel->setText(m_lastError);
        return false;
    }
    if (local) {
        const auto model = m_catalog.modelById(selectedModelId());
        const QString language = selectedLanguageCode();
        if (model && !model->languages.isEmpty() && !effectiveCatalogLanguage(*model, language)) {
            m_lastError = model->supportsLanguageDetection
                ? QStringLiteral("Language “%1” is not supported by local model “%2”. Choose Auto detect or a supported language before saving.")
                      .arg(language, model->name)
                : QStringLiteral("Language “%1” is not supported by local model “%2”. Choose a supported language before saving.")
                      .arg(language, model->name);
            m_modelStatusLabel->setText(m_lastError);
            return false;
        }
    }

    KwisprSettings settings = settingsFromWidgets();
    QStringList errors;
    if (!settings.validate(&errors)) {
        m_lastError = errors.join(QStringLiteral("\n"));
        m_modelStatusLabel->setText(m_lastError);
        return false;
    }

    if (m_globalShortcut) {
        if (!m_globalShortcutDirty) {
            // Other KDE tools may change the registration while this dialog is open.
            // A clean shortcut field follows that authoritative value without writing it.
            setGlobalShortcutClean(m_globalShortcut->currentShortcut(), false);
        } else {
            QString shortcutError;
            if (!m_globalShortcut->applyShortcutIfCurrent(
                    m_globalShortcutEdit->keySequence(), m_globalShortcutBaseline,
                    &shortcutError)) {
                m_lastError = shortcutError;
                m_globalShortcutStatusLabel->setText(shortcutError);
                m_globalShortcutEdit->setFocus(Qt::OtherFocusReason);
                return false;
            }
            setGlobalShortcutClean(m_globalShortcut->currentShortcut(), true);
        }
    }

    if (m_env) {
        settings.writeTo(*m_env);
    }
    m_settings = settings;
    emit settingsSaved(m_settings);
    return true;
}

void SettingsDialog::accept()
{
    if (m_modelOperationBusy) {
        showBusyCloseStatus();
        return;
    }
    QDialog::accept();
}

void SettingsDialog::reject()
{
    if (m_modelOperationBusy) {
        showBusyCloseStatus();
        return;
    }
    QDialog::reject();
}

void SettingsDialog::closeEvent(QCloseEvent *event)
{
    if (m_modelOperationBusy) {
        showBusyCloseStatus();
        event->ignore();
        return;
    }
    QDialog::closeEvent(event);
}

void SettingsDialog::buildUi()
{
    setWindowTitle(QStringLiteral("KDE Whisper Settings"));
    auto *root = new QVBoxLayout(this);

    auto *backendGroup = new QGroupBox(QStringLiteral("Backend"), this);
    backendGroup->setObjectName(QStringLiteral("backendGroup"));
    m_backendForm = new QFormLayout(backendGroup);

    m_backendCombo = new QComboBox(backendGroup);
    m_backendCombo->setObjectName(QStringLiteral("backendCombo"));
    m_backendCombo->addItems({QStringLiteral("Local STT"), QStringLiteral("OpenAI"), QStringLiteral("OpenRouter")});
    auto *backendLabel = formLabel(QStringLiteral("Backend"), QStringLiteral("backendLabel"), backendGroup);
    m_backendForm->addRow(backendLabel, m_backendCombo);

    m_localSttSectionLabel = new QLabel(QStringLiteral("Local STT connection / server"), backendGroup);
    m_localSttSectionLabel->setObjectName(QStringLiteral("localSttSectionLabel"));
    QFont sectionFont = m_localSttSectionLabel->font();
    sectionFont.setBold(true);
    m_localSttSectionLabel->setFont(sectionFont);
    m_backendForm->addRow(m_localSttSectionLabel);

    m_apiUrlEdit = new QLineEdit(backendGroup);
    m_apiUrlEdit->setObjectName(QStringLiteral("apiUrlEdit"));
    m_apiUrlLabel = formLabel(QStringLiteral("API URL"), QStringLiteral("apiUrlLabel"), backendGroup);
    m_backendForm->addRow(m_apiUrlLabel, m_apiUrlEdit);

    m_resolvedUrlLabel = new QLabel(backendGroup);
    m_resolvedUrlLabel->setObjectName(QStringLiteral("resolvedLocalSttUrlLabel"));
    m_resolvedUrlLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_resolvedUrlLabel->setWordWrap(true);
    m_resolvedUrlFieldLabel = formLabel(QStringLiteral("Resolved API URL"), QStringLiteral("resolvedLocalSttUrlFieldLabel"), backendGroup);
    m_backendForm->addRow(m_resolvedUrlFieldLabel, m_resolvedUrlLabel);

    m_localSttHostEdit = new QLineEdit(backendGroup);
    m_localSttHostEdit->setObjectName(QStringLiteral("localSttHostEdit"));
    m_localSttHostLabel = formLabel(QStringLiteral("Listen address"), QStringLiteral("localSttHostLabel"), backendGroup);
    m_backendForm->addRow(m_localSttHostLabel, m_localSttHostEdit);
    m_localSttPortSpin = new QSpinBox(backendGroup);
    m_localSttPortSpin->setObjectName(QStringLiteral("localSttPortSpin"));
    m_localSttPortSpin->setRange(1, 65535);
    m_localSttPortLabel = formLabel(QStringLiteral("Listen port"), QStringLiteral("localSttPortLabel"), backendGroup);
    m_backendForm->addRow(m_localSttPortLabel, m_localSttPortSpin);
    m_localSttAllowLanCheck = new QCheckBox(QStringLiteral("Allow LAN access (bind 0.0.0.0)"), backendGroup);
    m_localSttAllowLanCheck->setObjectName(QStringLiteral("localSttAllowLanCheck"));
    m_backendForm->addRow(QString(), m_localSttAllowLanCheck);

    m_apiKeyEdit = new QLineEdit(backendGroup);
    m_apiKeyEdit->setObjectName(QStringLiteral("apiKeyEdit"));
    m_apiKeyEdit->setEchoMode(QLineEdit::Password);
    m_apiKeyLabel = formLabel(QStringLiteral("API key"), QStringLiteral("apiKeyLabel"), backendGroup);
    m_backendForm->addRow(m_apiKeyLabel, m_apiKeyEdit);

    m_modelEdit = new QLineEdit(backendGroup);
    m_modelEdit->setObjectName(QStringLiteral("modelEdit"));
    m_modelLabel = formLabel(QStringLiteral("Model"), QStringLiteral("modelLabel"), backendGroup);
    m_backendForm->addRow(m_modelLabel, m_modelEdit);

    m_localModelRow = new QWidget(backendGroup);
    m_localModelRow->setObjectName(QStringLiteral("localModelRow"));
    auto *modelGrid = new QGridLayout(m_localModelRow);
    modelGrid->setContentsMargins(0, 0, 0, 0);
    modelGrid->setHorizontalSpacing(8);
    modelGrid->setVerticalSpacing(4);

    m_localModelCombo = new QComboBox(m_localModelRow);
    m_localModelCombo->setObjectName(QStringLiteral("localModelCombo"));
    m_localModelCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_localModelCombo->setMinimumContentsLength(24);
    m_downloadButton = new QPushButton(QStringLiteral("Download here"), m_localModelRow);
    m_downloadButton->setObjectName(QStringLiteral("localModelDownloadButton"));
    m_deleteButton = new QPushButton(QStringLiteral("Delete local"), m_localModelRow);
    m_deleteButton->setObjectName(QStringLiteral("localModelDeleteButton"));
    modelGrid->addWidget(m_localModelCombo, 0, 0);
    modelGrid->addWidget(m_downloadButton, 0, 1);
    modelGrid->addWidget(m_deleteButton, 0, 2);

    auto *progressStatus = new QWidget(m_localModelRow);
    auto *progressStatusLayout = new QVBoxLayout(progressStatus);
    progressStatusLayout->setContentsMargins(0, 0, 0, 0);
    progressStatusLayout->setSpacing(2);
    m_modelBusyIndicator = new QProgressBar(progressStatus);
    m_modelBusyIndicator->setObjectName(QStringLiteral("localModelBusyIndicator"));
    m_modelBusyIndicator->setRange(0, 0);
    m_modelBusyIndicator->setTextVisible(false);
    m_modelStatusLabel = new QLabel(QStringLiteral("Download/Delete manages model files on this computer only."), progressStatus);
    m_modelStatusLabel->setObjectName(QStringLiteral("modelOperationStatusLabel"));
    m_modelStatusLabel->setWordWrap(true);
    progressStatusLayout->addWidget(m_modelBusyIndicator);
    progressStatusLayout->addWidget(m_modelStatusLabel);
    modelGrid->addWidget(progressStatus, 1, 0);

    m_modelDownloadPercentLabel = new QLabel(m_localModelRow);
    m_modelDownloadPercentLabel->setObjectName(QStringLiteral("modelDownloadPercentLabel"));
    m_modelDownloadPercentLabel->setAlignment(Qt::AlignHCenter | Qt::AlignTop);
    m_modelDownloadEtaLabel = new QLabel(m_localModelRow);
    m_modelDownloadEtaLabel->setObjectName(QStringLiteral("modelDownloadEtaLabel"));
    m_modelDownloadEtaLabel->setAlignment(Qt::AlignHCenter | Qt::AlignTop);
    modelGrid->addWidget(m_modelDownloadPercentLabel, 1, 1);
    modelGrid->addWidget(m_modelDownloadEtaLabel, 1, 2);
    modelGrid->setColumnStretch(0, 1);
    m_modelDownloadPercentLabel->hide();
    m_modelDownloadEtaLabel->hide();

    m_localModelLabel = formLabel(QStringLiteral("Local model"), QStringLiteral("localModelLabel"), backendGroup);
    m_backendForm->addRow(m_localModelLabel, m_localModelRow);

    m_languageCombo = new QComboBox(backendGroup);
    m_languageCombo->setObjectName(QStringLiteral("languageEdit"));
    m_languageLabel = formLabel(QStringLiteral("Language"), QStringLiteral("languageLabel"), backendGroup);
    m_backendForm->addRow(m_languageLabel, m_languageCombo);

    m_promptEdit = new QPlainTextEdit(backendGroup);
    m_promptEdit->setObjectName(QStringLiteral("promptEdit"));
    m_promptEdit->setMinimumHeight(80);
    m_promptLabel = formLabel(QStringLiteral("Prompt"), QStringLiteral("promptLabel"), backendGroup);
    m_backendForm->addRow(m_promptLabel, m_promptEdit);
    root->addWidget(backendGroup);

    auto *pasteGroup = new QGroupBox(QStringLiteral("Paste"), this);
    pasteGroup->setObjectName(QStringLiteral("pasteGroup"));
    auto *pasteForm = new QFormLayout(pasteGroup);
    m_autopasteCheck = new QCheckBox(QStringLiteral("Paste automatically after transcription"), pasteGroup);
    m_autopasteCheck->setObjectName(QStringLiteral("autopasteCheck"));
    pasteForm->addRow(QString(), m_autopasteCheck);

    m_pasteHotkeyCombo = new QComboBox(pasteGroup);
    m_pasteHotkeyCombo->setObjectName(QStringLiteral("pasteHotkeyCombo"));
    m_pasteHotkeyCombo->setEditable(true);
    m_pasteHotkeyCombo->addItems({QStringLiteral("shift-insert"), QStringLiteral("ctrl-v"), QStringLiteral("ctrl-shift-v")});
    pasteForm->addRow(QStringLiteral("Paste hotkey"), m_pasteHotkeyCombo);

    m_autopasteDelaySpin = new QDoubleSpinBox(pasteGroup);
    m_autopasteDelaySpin->setObjectName(QStringLiteral("autopasteDelaySpin"));
    m_autopasteDelaySpin->setRange(0.0, 10.0);
    m_autopasteDelaySpin->setDecimals(2);
    m_autopasteDelaySpin->setSingleStep(0.05);
    pasteForm->addRow(QStringLiteral("Paste delay"), m_autopasteDelaySpin);
    root->addWidget(pasteGroup);

    auto *shortcutGroup = new QGroupBox(QStringLiteral("Global dictation shortcut"), this);
    shortcutGroup->setObjectName(QStringLiteral("globalShortcutGroup"));
    auto *shortcutForm = new QFormLayout(shortcutGroup);
    m_globalShortcutEdit = new QKeySequenceEdit(shortcutGroup);
    m_globalShortcutEdit->setObjectName(QStringLiteral("globalShortcutEdit"));
    m_globalShortcutEdit->setClearButtonEnabled(true);
    m_globalShortcutEdit->setMaximumSequenceLength(1);
    m_globalShortcutEdit->setAccessibleName(QStringLiteral("Global dictation shortcut"));
    auto *shortcutLabel = formLabel(QStringLiteral("Shortcut"),
                                    QStringLiteral("globalShortcutLabel"), shortcutGroup);
    shortcutLabel->setBuddy(m_globalShortcutEdit);
    shortcutForm->addRow(shortcutLabel, m_globalShortcutEdit);
    auto *shortcutHelp = new QLabel(
        QStringLiteral("Record one key combination, or clear it to disable the native shortcut. "
                       "Apply saves it in KDE Global Shortcuts immediately; Kwispr never takes a conflicting shortcut."),
        shortcutGroup);
    shortcutHelp->setObjectName(QStringLiteral("globalShortcutHelpLabel"));
    shortcutHelp->setWordWrap(true);
    shortcutForm->addRow(QString(), shortcutHelp);
    m_globalShortcutStatusLabel = new QLabel(shortcutGroup);
    m_globalShortcutStatusLabel->setObjectName(QStringLiteral("globalShortcutStatusLabel"));
    m_globalShortcutStatusLabel->setWordWrap(true);
    shortcutForm->addRow(QString(), m_globalShortcutStatusLabel);
    root->addWidget(shortcutGroup);

    m_vadGroup = new QGroupBox(QStringLiteral("Voice activity detection"), this);
    m_vadGroup->setObjectName(QStringLiteral("vadGroup"));
    auto *vadForm = new QFormLayout(m_vadGroup);
    m_vadEnabledCheck = new QCheckBox(QStringLiteral("Enable VAD"), m_vadGroup);
    m_vadEnabledCheck->setObjectName(QStringLiteral("vadEnabledCheck"));
    vadForm->addRow(QString(), m_vadEnabledCheck);

    m_vadProviderCombo = new QComboBox(m_vadGroup);
    m_vadProviderCombo->setObjectName(QStringLiteral("vadProviderCombo"));
    m_vadProviderCombo->addItems({QStringLiteral("energy"), QStringLiteral("silero")});
    vadForm->addRow(QStringLiteral("Provider"), m_vadProviderCombo);

    m_vadModelPathEdit = new QLineEdit(m_vadGroup);
    m_vadModelPathEdit->setObjectName(QStringLiteral("vadModelPathEdit"));
    m_vadModelPathLabel = formLabel(QStringLiteral("Silero model"), QStringLiteral("vadModelPathLabel"), m_vadGroup);
    vadForm->addRow(m_vadModelPathLabel, m_vadModelPathEdit);

    m_vadThresholdSpin = new QDoubleSpinBox(m_vadGroup);
    m_vadThresholdSpin->setObjectName(QStringLiteral("vadThresholdSpin"));
    m_vadThresholdSpin->setRange(0.0, 100.0);
    m_vadThresholdSpin->setDecimals(4);
    m_vadThresholdSpin->setSingleStep(0.05);
    vadForm->addRow(QStringLiteral("Threshold"), m_vadThresholdSpin);

    m_vadFrameMsEdit = new QLineEdit(m_vadGroup);
    m_vadFrameMsEdit->setObjectName(QStringLiteral("vadFrameMsEdit"));
    vadForm->addRow(QStringLiteral("Frame ms"), m_vadFrameMsEdit);
    root->addWidget(m_vadGroup);

    m_buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Apply | QDialogButtonBox::Cancel, this);
    m_buttons->setObjectName(QStringLiteral("buttonBox"));
    root->addWidget(m_buttons);

    connect(m_backendCombo, &QComboBox::currentTextChanged, this, &SettingsDialog::applyBackendPreset);
    connect(m_localModelCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this]() {
        populateLanguageChoices(selectedLanguageCode());
        updateBackendVisibility();
    });
    connect(m_globalShortcutEdit, &QKeySequenceEdit::keySequenceChanged,
            this, [this](const QKeySequence &sequence) {
        m_globalShortcutDirty = true;
        m_globalShortcutStatusLabel->setText(
            sequence.isEmpty()
                ? QStringLiteral("The global shortcut will be disabled when you apply settings.")
                : QStringLiteral("Press Apply to register %1 globally.")
                      .arg(sequence.toString(QKeySequence::NativeText)));
    });
    connect(m_vadEnabledCheck, &QCheckBox::toggled, this, [this]() {
        updateVadControls();
    });
    connect(m_vadProviderCombo, &QComboBox::currentTextChanged, this, [this]() {
        updateVadControls();
    });
    connect(m_localSttHostEdit, &QLineEdit::textChanged, this, [this](const QString &host) {
        QHostAddress address;
        const bool lanAddress = address.setAddress(host.trimmed()) && !address.isLoopback();
        const QSignalBlocker blocker(m_localSttAllowLanCheck);
        m_localSttAllowLanCheck->setChecked(lanAddress);
    });
    connect(m_localSttPortSpin, qOverload<int>(&QSpinBox::valueChanged), this, [this]() {
        m_localSttPortNeedsCorrection = false;
    });
    connect(m_localSttPortSpin, &QSpinBox::editingFinished, this, [this]() {
        m_localSttPortNeedsCorrection = false;
    });
    connect(m_localSttAllowLanCheck, &QCheckBox::toggled, this, [this](bool enabled) {
        if (enabled) {
            m_localSttHostEdit->setText(QStringLiteral("0.0.0.0"));
        } else {
            QHostAddress address;
            if (address.setAddress(m_localSttHostEdit->text().trimmed()) && !address.isLoopback()) {
                m_localSttHostEdit->setText(QStringLiteral("127.0.0.1"));
            }
        }
        updateBackendVisibility();
    });
    connect(m_apiUrlEdit, &QLineEdit::textChanged, this, [this](const QString &url) {
        m_resolvedUrlLabel->setText(url.trimmed());
    });
    connect(m_downloadButton, &QPushButton::clicked, this, &SettingsDialog::startModelDownload);
    connect(m_deleteButton, &QPushButton::clicked, this, &SettingsDialog::confirmAndDeleteModel);
    connect(m_buttons->button(QDialogButtonBox::Apply), &QPushButton::clicked, this, &SettingsDialog::save);
    connect(m_buttons, &QDialogButtonBox::accepted, this, [this]() {
        if (save()) {
            accept();
        }
    });
    connect(m_buttons, &QDialogButtonBox::rejected, this, &SettingsDialog::reject);
}

void SettingsDialog::loadFromSettings(const KwisprSettings &settings)
{
    const QString backendLabel = backendLabelForSettings(settings);
    const QSignalBlocker blocker(m_backendCombo);
    m_backendCombo->setCurrentText(backendLabel);
    m_apiUrlEdit->setText(settings.apiUrl);
    m_localSttHostEdit->setText(settings.localSttHost);
    {
        const QSignalBlocker portBlocker(m_localSttPortSpin);
        m_localSttPortSpin->setValue(settings.localSttPort);
    }
    m_localSttPortNeedsCorrection = !settings.localSttPortValid
        || settings.localSttPort < 1 || settings.localSttPort > 65535;
    const QSignalBlocker allowLanBlocker(m_localSttAllowLanCheck);
    m_localSttAllowLanCheck->setChecked(settings.localSttAllowLan);
    m_resolvedUrlLabel->setText(settings.apiUrl);
    QString apiKey = settings.apiKey;
    if (backendLabel == QLatin1String("Local STT") && apiKey.isEmpty() && m_env
        && m_env->contains(QStringLiteral("KWISPR_API_KEY"))) {
        apiKey = m_env->value(QStringLiteral("KWISPR_API_KEY"));
    }
    m_apiKeyEdit->setText(apiKey);
    m_modelEdit->setText(settings.model);
    if (backendLabel == QLatin1String("Local STT")) {
        populateModels(settings.model);
    }
    populateLanguageChoices(settings.language);
    m_promptEdit->setPlainText(settings.transcriptionPrompt);
    m_autopasteCheck->setChecked(settings.autopaste);
    m_pasteHotkeyCombo->setCurrentText(settings.pasteHotkey);
    m_autopasteDelaySpin->setValue(settings.autopasteDelay);
    m_vadEnabledCheck->setChecked(settings.vadEnabled);
    m_vadProviderCombo->setCurrentText(settings.vadProvider);
    m_vadModelPathEdit->setText(settings.vadModelPath);
    m_vadThresholdSpin->setValue(settings.vadThreshold);
    m_vadFrameMsEdit->setText(QString::number(settings.vadFrameMs));

    if (m_globalShortcut) {
        setGlobalShortcutClean(m_globalShortcut->currentShortcut(), false);
        m_globalShortcutEdit->setEnabled(true);
    } else {
        m_globalShortcutEdit->setEnabled(false);
        m_globalShortcutStatusLabel->setText(QStringLiteral("Global shortcut management is available from the running tray."));
    }

    m_activeBackend = backendLabel;
    saveActiveBackendDraft();
    updateBackendVisibility();
}

void SettingsDialog::setGlobalShortcutClean(const QKeySequence &shortcut, bool justApplied)
{
    m_globalShortcutBaseline = shortcut;
    m_globalShortcutDirty = false;
    const QSignalBlocker blocker(m_globalShortcutEdit);
    m_globalShortcutEdit->setKeySequence(shortcut);
    if (shortcut.isEmpty()) {
        m_globalShortcutStatusLabel->setText(
            justApplied
                ? QStringLiteral("Global dictation shortcut disabled. Use ~/.local/bin/kwispr toggle as a fallback.")
                : QStringLiteral("No native global shortcut is registered. ~/.local/bin/kwispr toggle remains available."));
    } else {
        m_globalShortcutStatusLabel->setText(
            justApplied
                ? QStringLiteral("Registered globally as %1.").arg(shortcut.toString(QKeySequence::NativeText))
                : QStringLiteral("Currently registered globally as %1.").arg(shortcut.toString(QKeySequence::NativeText)));
    }
}

void SettingsDialog::populateModels(const QString &selectedModelId)
{
    const QString selection = selectedModelId.trimmed();
    const QSignalBlocker blocker(m_localModelCombo);
    m_localModelCombo->clear();
    for (const LocalModel &model : m_catalog.models) {
        const bool installed = m_installedModelIds.contains(model.id);
        const QString label = QStringLiteral("%1 (%2)").arg(model.name, installed ? QStringLiteral("installed") : QStringLiteral("not installed"));
        m_localModelCombo->addItem(label, model.id);
    }
    if (!selection.isEmpty() && !m_catalog.modelById(selection)) {
        m_localModelCombo->addItem(QStringLiteral("%1 (not in catalog)").arg(selection), selection);
    }
    m_localModelCombo->setCurrentIndex(selection.isEmpty() ? -1 : m_localModelCombo->findData(selection));
    updateModelControls();
}

void SettingsDialog::populateLanguageChoices(const QString &languageCode)
{
    const QString requested = languageCode.trimmed();
    const QString backend = m_backendCombo->currentText();
    const QSignalBlocker blocker(m_languageCombo);
    m_languageCombo->clear();

    if (backend == QLatin1String("Local STT")) {
        const auto model = m_catalog.modelById(selectedModelId());
        if (model) {
            m_languageCombo->setEditable(false);
            if (model->supportsLanguageDetection) {
                m_languageCombo->addItem(
                    QStringLiteral("Auto detect (recommended for mixed-language speech)"), QString());
            }
            for (const QString &code : model->languages) {
                m_languageCombo->addItem(languageLabel(code), code);
            }

            int index = -1;
            if (!model->languages.isEmpty()) {
                const auto effective = effectiveCatalogLanguage(*model, requested);
                if (effective) {
                    index = m_languageCombo->findData(*effective);
                } else {
                    m_languageCombo->addItem(
                        QStringLiteral("Unsupported current value: %1 — choose another language").arg(requested),
                        requested);
                    index = m_languageCombo->count() - 1;
                }
            } else if (m_languageCombo->count() > 0) {
                index = 0;
            }
            m_languageCombo->setCurrentIndex(index);
            return;
        }

        m_languageCombo->setEditable(true);
        if (requested.isEmpty()) {
            m_languageCombo->addItem(QStringLiteral("Auto"), QString());
        } else {
            m_languageCombo->addItem(requested, requested);
        }
        m_languageCombo->setCurrentIndex(0);
        return;
    }

    m_languageCombo->setEditable(true);
    if (backend == QLatin1String("OpenAI")) {
        m_languageCombo->addItem(QStringLiteral("Auto"), QString());
        m_languageCombo->addItem(languageLabel(QStringLiteral("en")), QStringLiteral("en"));
        m_languageCombo->addItem(languageLabel(QStringLiteral("ru")), QStringLiteral("ru"));
        int index = m_languageCombo->findData(requested);
        if (index < 0 && !requested.isEmpty()) {
            m_languageCombo->addItem(requested, requested);
            index = m_languageCombo->count() - 1;
        }
        m_languageCombo->setCurrentIndex(index < 0 ? 0 : index);
        return;
    }

    if (requested.isEmpty()) {
        m_languageCombo->addItem(QStringLiteral("Auto"), QString());
    } else {
        m_languageCombo->addItem(requested, requested);
    }
    m_languageCombo->setCurrentIndex(0);
}

QString SettingsDialog::selectedLanguageCode() const
{
    const int index = m_languageCombo->currentIndex();
    if (index >= 0 && m_languageCombo->currentText() == m_languageCombo->itemText(index)) {
        return m_languageCombo->itemData(index).toString().trimmed();
    }
    return m_languageCombo->currentText().trimmed();
}

QString SettingsDialog::formatEta(qint64 seconds)
{
    if (seconds < 60) {
        return QStringLiteral("<1m");
    }
    if (seconds < 3600) {
        const qint64 minutes = seconds / 60;
        const qint64 remainder = seconds % 60;
        return remainder == 0 ? QStringLiteral("%1m").arg(minutes)
                              : QStringLiteral("%1m %2s").arg(minutes).arg(remainder);
    }
    const qint64 hours = seconds / 3600;
    const qint64 minutes = (seconds % 3600) / 60;
    return minutes == 0 ? QStringLiteral("%1h").arg(hours)
                        : QStringLiteral("%1h %2m").arg(hours).arg(minutes);
}

void SettingsDialog::applyBackendPreset(const QString &backendLabel)
{
    if (backendLabel == m_activeBackend) {
        updateBackendVisibility();
        return;
    }
    saveActiveBackendDraft();
    loadBackendDraft(backendLabel);
    m_activeBackend = backendLabel;
    updateBackendVisibility();
}

void SettingsDialog::setBackendRowVisible(QWidget *field, QLabel *label, bool visible)
{
    field->setVisible(visible);
    label->setVisible(visible);
}

void SettingsDialog::updateBackendVisibility()
{
    const QString backend = m_backendCombo->currentText();
    const bool local = backend == QLatin1String("Local STT");
    const bool openAi = backend == QLatin1String("OpenAI");
    const bool openRouter = backend == QLatin1String("OpenRouter");
    const auto localModel = m_catalog.modelById(selectedModelId());
    const bool localLanguageVisible = !localModel || !localModel->languages.isEmpty()
        || localModel->supportsLanguageSelection || localModel->supportsLanguageDetection;

    m_localSttSectionLabel->setVisible(local);
    setBackendRowVisible(m_apiUrlEdit, m_apiUrlLabel, true);
    m_apiUrlEdit->setReadOnly(false);
    m_resolvedUrlLabel->setVisible(local);
    m_resolvedUrlFieldLabel->setVisible(local);
    m_localSttHostEdit->setVisible(local && m_localRuntimeInstalled);
    m_localSttHostLabel->setVisible(local && m_localRuntimeInstalled);
    m_localSttPortSpin->setVisible(local && m_localRuntimeInstalled);
    m_localSttPortLabel->setVisible(local && m_localRuntimeInstalled);
    m_localSttAllowLanCheck->setVisible(local && m_localRuntimeInstalled);
    setBackendRowVisible(m_apiKeyEdit, m_apiKeyLabel, !local);
    setBackendRowVisible(m_modelEdit, m_modelLabel, !local);
    setBackendRowVisible(m_localModelRow, m_localModelLabel, local);
    setBackendRowVisible(m_languageCombo, m_languageLabel, openAi || (local && localLanguageVisible));
    setBackendRowVisible(m_promptEdit, m_promptLabel, openRouter);
    m_vadGroup->setVisible(local && m_localRuntimeInstalled);
    updateVadControls();
    updateModelControls();
}

void SettingsDialog::updateModelControls()
{
    const bool local = m_backendCombo->currentText() == QLatin1String("Local STT");
    const QString modelId = selectedModelId();
    const bool installed = m_installedModelIds.contains(modelId);
    const bool manageable = local && m_modelManager && selectedModelIsCatalogModel() && !m_modelOperationBusy;

    m_backendCombo->setEnabled(!m_modelOperationBusy);
    m_localModelCombo->setEnabled(local && !m_modelOperationBusy);
    m_downloadButton->setEnabled(manageable && !installed);
    m_deleteButton->setEnabled(manageable && installed);
    m_modelBusyIndicator->setVisible(local && m_modelOperationBusy);
    m_buttons->setEnabled(!m_modelOperationBusy);
}

void SettingsDialog::updateVadControls()
{
    const bool local = m_backendCombo->currentText() == QLatin1String("Local STT");
    const bool enabled = local && m_vadEnabledCheck->isChecked();
    const bool silero = enabled && m_vadProviderCombo->currentText().compare(QStringLiteral("silero"), Qt::CaseInsensitive) == 0;

    m_vadProviderCombo->setEnabled(enabled);
    m_vadThresholdSpin->setEnabled(enabled);
    m_vadFrameMsEdit->setEnabled(enabled);
    m_vadModelPathEdit->setVisible(silero);
    m_vadModelPathLabel->setVisible(silero);
    m_vadModelPathEdit->setEnabled(silero);
}

void SettingsDialog::showBusyCloseStatus()
{
    m_lastError = QStringLiteral("Wait for the current model operation to finish before closing settings.");
    m_modelStatusLabel->setText(m_lastError);
}

bool SettingsDialog::selectedModelIsCatalogModel() const
{
    return m_catalog.isValid && m_catalog.modelById(selectedModelId()).has_value();
}

void SettingsDialog::saveActiveBackendDraft()
{
    if (m_activeBackend.isEmpty()) {
        return;
    }
    m_backendDrafts.insert(m_activeBackend, BackendDraft{
        m_apiUrlEdit->text(),
        m_apiKeyEdit->text(),
        m_activeBackend == QLatin1String("Local STT") ? selectedModelId() : m_modelEdit->text(),
        selectedLanguageCode(),
        m_promptEdit->toPlainText(),
    });
}

void SettingsDialog::loadBackendDraft(const QString &backendLabel)
{
    BackendDraft draft;
    if (m_backendDrafts.contains(backendLabel)) {
        draft = m_backendDrafts.value(backendLabel);
    } else {
        draft.apiKey = m_apiKeyEdit->text();
        draft.language = selectedLanguageCode();
        draft.prompt = m_promptEdit->toPlainText();
        if (backendLabel == QLatin1String("Local STT")) {
            QUrl localUrl(QString::fromLatin1(LocalUrl));
            localUrl.setPort(m_localSttPortSpin->value());
            draft.apiUrl = localUrl.toString();
            draft.model = !selectedModelId().isEmpty()
                ? selectedModelId()
                : (m_catalog.models.isEmpty() ? QString() : m_catalog.models.constFirst().id);
        } else if (backendLabel == QLatin1String("OpenAI")) {
            draft.apiUrl = QString::fromLatin1(OpenAiUrl);
            draft.model = QStringLiteral("whisper-1");
        } else {
            draft.apiUrl = QString::fromLatin1(OpenRouterUrl);
            draft.model = QStringLiteral("openai/gpt-4o-mini-transcribe");
        }
        m_backendDrafts.insert(backendLabel, draft);
    }

    m_apiUrlEdit->setText(draft.apiUrl);
    m_apiKeyEdit->setText(draft.apiKey);
    m_modelEdit->setText(draft.model);
    if (backendLabel == QLatin1String("Local STT")) {
        populateModels(draft.model);
    }
    populateLanguageChoices(draft.language);
    m_promptEdit->setPlainText(draft.prompt);
}

QString SettingsDialog::selectedModelId() const
{
    return m_localModelCombo->currentData().toString();
}

QString SettingsDialog::selectedModelName() const
{
    const auto model = m_catalog.modelById(selectedModelId());
    return model ? model->name : selectedModelId();
}

void SettingsDialog::startModelDownload()
{
    if (!m_modelManager || m_modelOperationBusy) {
        return;
    }
    const QString modelId = selectedModelId();
    if (!m_modelManager->startDownload(modelId)) {
        m_modelStatusLabel->setText(QStringLiteral("Could not start model download."));
    }
}

void SettingsDialog::confirmAndDeleteModel()
{
    if (!m_modelManager || m_modelOperationBusy) {
        return;
    }
    const QString modelId = selectedModelId();
    const QString name = selectedModelName();
    QString message = QStringLiteral("Delete the downloaded model “%1”?\n\nOnly its catalog-managed default GGUF will be removed.").arg(name);
    const bool deletingConfiguredLocalModel = backendLabelForSettings(m_settings) == QLatin1String("Local STT")
        && m_settings.model == modelId;
    if (deletingConfiguredLocalModel) {
        message += QStringLiteral("\n\nThis is the currently configured Local STT model. You must select and apply another installed model before future server starts.");
    }
    const auto answer = QMessageBox::question(
        this,
        QStringLiteral("Delete local model"),
        message,
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (answer == QMessageBox::Yes && !m_modelManager->startDelete(modelId)) {
        m_modelStatusLabel->setText(QStringLiteral("Could not start model deletion."));
    }
}

void SettingsDialog::modelOperationStarted(const QString &operation, const QString &modelId)
{
    m_modelOperationBusy = true;
    m_activeModelOperation = operation;
    m_activeModelId = modelId;
    const auto model = m_catalog.modelById(modelId);
    const QString name = model ? model->name : modelId;
    if (operation == QLatin1String("download")) {
        m_downloadElapsed.restart();
        m_modelBusyIndicator->setRange(0, 0);
        m_modelDownloadPercentLabel->setText(QStringLiteral("0%"));
        m_modelDownloadEtaLabel->setText(QStringLiteral("Calculating"));
        m_modelDownloadPercentLabel->show();
        m_modelDownloadEtaLabel->show();
        m_modelStatusLabel->setText(QStringLiteral("Downloading %1…").arg(name));
    } else {
        m_modelBusyIndicator->setRange(0, 0);
        m_modelDownloadPercentLabel->hide();
        m_modelDownloadEtaLabel->hide();
        m_modelStatusLabel->setText(QStringLiteral("Deleting %1…").arg(name));
    }
    updateModelControls();
}

void SettingsDialog::modelDownloadStatus(const QString &modelId,
                                         const QString &status,
                                         qint64 bytesDone,
                                         qint64 bytesTotal)
{
    if (!m_modelOperationBusy || m_activeModelOperation != QLatin1String("download")
        || modelId != m_activeModelId) {
        return;
    }
    if (status == QLatin1String("reset") || status == QLatin1String("attempt")) {
        m_downloadElapsed.restart();
        m_modelBusyIndicator->setRange(0, 0);
        m_modelDownloadPercentLabel->setText(QStringLiteral("0%"));
        m_modelDownloadEtaLabel->setText(QStringLiteral("Calculating"));
        m_modelStatusLabel->setText(status == QLatin1String("reset")
                                        ? QStringLiteral("Retrying another download source…")
                                        : QStringLiteral("Connecting to download source…"));
    } else if (status == QLatin1String("downloading")) {
        m_modelStatusLabel->setText(QStringLiteral("Downloading…"));
    } else if (status == QLatin1String("checksum-verification")) {
        m_modelBusyIndicator->setRange(0, 100);
        m_modelBusyIndicator->setValue(100);
        m_modelDownloadPercentLabel->setText(QStringLiteral("100%"));
        m_modelDownloadEtaLabel->setText(QStringLiteral("Verifying"));
        m_modelStatusLabel->setText(QStringLiteral("Verifying checksum…"));
    } else if (status == QLatin1String("installing")) {
        m_modelStatusLabel->setText(QStringLiteral("Installing verified model…"));
        m_modelDownloadEtaLabel->setText(QStringLiteral("Installing"));
    } else if (status == QLatin1String("already-installed")) {
        m_modelBusyIndicator->setRange(0, 100);
        m_modelBusyIndicator->setValue(100);
        m_modelDownloadPercentLabel->setText(QStringLiteral("100%"));
        m_modelDownloadEtaLabel->setText(QStringLiteral("Done"));
        m_modelStatusLabel->setText(QStringLiteral("Model is already installed."));
    } else if (status == QLatin1String("done")) {
        m_modelDownloadEtaLabel->setText(QStringLiteral("Done"));
    } else if (status == QLatin1String("failed")) {
        m_modelDownloadEtaLabel->setText(QStringLiteral("Failed"));
    }
    Q_UNUSED(bytesDone);
    Q_UNUSED(bytesTotal);
}

void SettingsDialog::modelDownloadProgress(const QString &modelId,
                                           qint64 bytesDone,
                                           qint64 bytesTotal)
{
    if (!m_modelOperationBusy || m_activeModelOperation != QLatin1String("download")
        || modelId != m_activeModelId || bytesTotal <= 0 || bytesDone < 0 || bytesDone > bytesTotal) {
        return;
    }
    const int percent = qBound(0, static_cast<int>((100.0 * bytesDone) / bytesTotal), 100);
    m_modelBusyIndicator->setRange(0, 100);
    m_modelBusyIndicator->setValue(percent);
    m_modelDownloadPercentLabel->setText(QStringLiteral("%1%").arg(percent));
    if (bytesDone <= 0 || !m_downloadElapsed.isValid() || m_downloadElapsed.elapsed() <= 0) {
        m_modelDownloadEtaLabel->setText(QStringLiteral("Calculating"));
        return;
    }
    const double bytesPerSecond = bytesDone * 1000.0 / m_downloadElapsed.elapsed();
    if (bytesPerSecond <= 0.0) {
        m_modelDownloadEtaLabel->setText(QStringLiteral("Calculating"));
        return;
    }
    const qint64 remainingSeconds = qCeil((bytesTotal - bytesDone) / bytesPerSecond);
    m_modelDownloadEtaLabel->setText(formatEta(remainingSeconds));
}

QString SettingsDialog::operationError(const QString &stdoutText, const QString &stderrText) const
{
    QString detail = stderrText.trimmed();
    if (detail.isEmpty()) {
        detail = stdoutText.trimmed();
    }
    detail.replace(QLatin1Char('\n'), QLatin1Char(' '));
    if (detail.size() > 300) {
        detail = detail.left(297) + QStringLiteral("…");
    }
    return detail.isEmpty() ? QStringLiteral("the model helper exited with an error") : detail;
}

void SettingsDialog::modelOperationFinished(const QString &operation,
                                             const QString &modelId,
                                             bool success,
                                             const QString &stdoutText,
                                             const QString &stderrText)
{
    m_modelOperationBusy = false;
    m_activeModelOperation.clear();
    m_activeModelId.clear();
    const auto model = m_catalog.modelById(modelId);
    const QString name = model ? model->name : modelId;
    if (success) {
        if (operation == QLatin1String("download")) {
            m_installedModelIds.insert(modelId);
            m_modelBusyIndicator->setValue(100);
            m_modelDownloadPercentLabel->setText(QStringLiteral("100%"));
            m_modelDownloadEtaLabel->setText(QStringLiteral("Done"));
            m_modelStatusLabel->setText(QStringLiteral("Done — Downloaded %1.").arg(name));
        } else if (operation == QLatin1String("delete")) {
            m_installedModelIds.remove(modelId);
            m_modelDownloadPercentLabel->hide();
            m_modelDownloadEtaLabel->hide();
            m_modelStatusLabel->setText(QStringLiteral("Deleted %1.").arg(name));
        }
        populateModels(modelId);
    } else {
        const QString action = operation == QLatin1String("delete") ? QStringLiteral("Delete") : QStringLiteral("Download");
        if (operation == QLatin1String("download")) {
            m_modelDownloadEtaLabel->setText(QStringLiteral("Failed"));
        } else {
            m_modelDownloadPercentLabel->hide();
            m_modelDownloadEtaLabel->hide();
        }
        m_modelStatusLabel->setText(QStringLiteral("%1 failed: %2").arg(action, operationError(stdoutText, stderrText)));
    }
    updateModelControls();
}

KwisprSettings SettingsDialog::settingsFromWidgets() const
{
    KwisprSettings settings = m_settings;
    const bool local = m_backendCombo->currentText() == QLatin1String("Local STT");
    settings.backend = backendValueForLabel(m_backendCombo->currentText());
    settings.apiUrl = m_apiUrlEdit->text().trimmed();
    settings.localSttHost = m_localSttHostEdit->text().trimmed();
    settings.localSttPort = m_localSttPortNeedsCorrection ? 0 : m_localSttPortSpin->value();
    settings.localSttPortValid = !m_localSttPortNeedsCorrection;
    settings.localSttAllowLan = m_localSttAllowLanCheck->isChecked();
    settings.localSttConfigured = local;
    settings.apiKey = m_apiKeyEdit->text();
    settings.model = local ? selectedModelId() : m_modelEdit->text().trimmed();
    settings.language = selectedLanguageCode();
    settings.transcriptionPrompt = m_promptEdit->toPlainText();
    settings.autopaste = m_autopasteCheck->isChecked();
    settings.pasteHotkey = m_pasteHotkeyCombo->currentText();
    settings.autopasteDelay = m_autopasteDelaySpin->value();
    settings.vadEnabled = m_vadEnabledCheck->isChecked();
    settings.vadProvider = m_vadProviderCombo->currentText();
    settings.vadModelPath = m_vadModelPathEdit->text().trimmed();
    settings.vadThreshold = m_vadThresholdSpin->value();
    bool ok = false;
    const int frameMs = m_vadFrameMsEdit->text().trimmed().toInt(&ok);
    settings.vadFrameMs = ok ? frameMs : 0;
    return settings;
}

QString SettingsDialog::backendLabelForSettings(const KwisprSettings &settings) const
{
    if (settings.backend == QLatin1String("openrouter-chat") || settings.apiUrl == QLatin1String(OpenRouterUrl)) {
        return QStringLiteral("OpenRouter");
    }
    if (settings.localSttConfigured || settings.apiUrl == QLatin1String(LocalUrl)) {
        return QStringLiteral("Local STT");
    }
    return QStringLiteral("OpenAI");
}
