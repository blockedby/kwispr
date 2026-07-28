#include "ui/SettingsDialog.h"

#include "models/ModelManager.h"

#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSignalBlocker>
#include <QVBoxLayout>

namespace {
constexpr const char *LocalUrl = "http://127.0.0.1:9000/v1/audio/transcriptions";
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
}

SettingsDialog::SettingsDialog(const KwisprSettings &settings,
                               const ModelCatalog &catalog,
                               const QStringList &installedModelIds,
                               EnvFile *env,
                               ModelManager *modelManager,
                               QWidget *parent)
    : QDialog(parent)
    , m_catalog(catalog)
    , m_installedModelIds(installedModelIds.begin(), installedModelIds.end())
    , m_env(env)
    , m_modelManager(modelManager)
    , m_settings(settings)
{
    buildUi();
    populateModels(QString());
    loadFromSettings(settings);

    if (m_modelManager) {
        connect(m_modelManager, &ModelManager::operationStarted,
                this, &SettingsDialog::modelOperationStarted);
        connect(m_modelManager, &ModelManager::operationFinished,
                this, &SettingsDialog::modelOperationFinished);
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

    KwisprSettings settings = settingsFromWidgets();
    QStringList errors;
    if (!settings.validate(&errors)) {
        m_lastError = errors.join(QStringLiteral("\n"));
        return false;
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

    m_apiUrlEdit = new QLineEdit(backendGroup);
    m_apiUrlEdit->setObjectName(QStringLiteral("apiUrlEdit"));
    m_apiUrlLabel = formLabel(QStringLiteral("API URL"), QStringLiteral("apiUrlLabel"), backendGroup);
    m_backendForm->addRow(m_apiUrlLabel, m_apiUrlEdit);

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
    auto *modelRowLayout = new QVBoxLayout(m_localModelRow);
    modelRowLayout->setContentsMargins(0, 0, 0, 0);
    modelRowLayout->setSpacing(4);

    auto *modelActionsLayout = new QHBoxLayout;
    m_localModelCombo = new QComboBox(m_localModelRow);
    m_localModelCombo->setObjectName(QStringLiteral("localModelCombo"));
    m_localModelCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_localModelCombo->setMinimumContentsLength(24);
    m_downloadButton = new QPushButton(QStringLiteral("Download"), m_localModelRow);
    m_downloadButton->setObjectName(QStringLiteral("localModelDownloadButton"));
    m_deleteButton = new QPushButton(QStringLiteral("Delete"), m_localModelRow);
    m_deleteButton->setObjectName(QStringLiteral("localModelDeleteButton"));
    modelActionsLayout->addWidget(m_localModelCombo, 1);
    modelActionsLayout->addWidget(m_downloadButton);
    modelActionsLayout->addWidget(m_deleteButton);
    modelRowLayout->addLayout(modelActionsLayout);

    auto *modelStatusLayout = new QHBoxLayout;
    m_modelBusyIndicator = new QProgressBar(m_localModelRow);
    m_modelBusyIndicator->setObjectName(QStringLiteral("localModelBusyIndicator"));
    m_modelBusyIndicator->setRange(0, 0);
    m_modelBusyIndicator->setTextVisible(false);
    m_modelBusyIndicator->setFixedWidth(48);
    m_modelStatusLabel = new QLabel(m_localModelRow);
    m_modelStatusLabel->setObjectName(QStringLiteral("modelOperationStatusLabel"));
    m_modelStatusLabel->setWordWrap(true);
    modelStatusLayout->addWidget(m_modelBusyIndicator);
    modelStatusLayout->addWidget(m_modelStatusLabel, 1);
    modelRowLayout->addLayout(modelStatusLayout);

    m_localModelLabel = formLabel(QStringLiteral("Local model"), QStringLiteral("localModelLabel"), backendGroup);
    m_backendForm->addRow(m_localModelLabel, m_localModelRow);

    m_languageEdit = new QLineEdit(backendGroup);
    m_languageEdit->setObjectName(QStringLiteral("languageEdit"));
    m_languageLabel = formLabel(QStringLiteral("Language"), QStringLiteral("languageLabel"), backendGroup);
    m_backendForm->addRow(m_languageLabel, m_languageEdit);

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
    vadForm->addRow(QStringLiteral("Silero model"), m_vadModelPathEdit);

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
        updateModelControls();
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
    m_languageEdit->setText(settings.language);
    m_promptEdit->setPlainText(settings.transcriptionPrompt);
    m_autopasteCheck->setChecked(settings.autopaste);
    m_pasteHotkeyCombo->setCurrentText(settings.pasteHotkey);
    m_autopasteDelaySpin->setValue(settings.autopasteDelay);
    m_vadEnabledCheck->setChecked(settings.vadEnabled);
    m_vadProviderCombo->setCurrentText(settings.vadProvider);
    m_vadModelPathEdit->setText(settings.vadModelPath);
    m_vadThresholdSpin->setValue(settings.vadThreshold);
    m_vadFrameMsEdit->setText(QString::number(settings.vadFrameMs));

    m_activeBackend = backendLabel;
    saveActiveBackendDraft();
    updateBackendVisibility();
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

    setBackendRowVisible(m_apiUrlEdit, m_apiUrlLabel, true);
    setBackendRowVisible(m_apiKeyEdit, m_apiKeyLabel, !local);
    setBackendRowVisible(m_modelEdit, m_modelLabel, !local);
    setBackendRowVisible(m_localModelRow, m_localModelLabel, local);
    setBackendRowVisible(m_languageEdit, m_languageLabel, local || openAi);
    setBackendRowVisible(m_promptEdit, m_promptLabel, openRouter);
    m_vadGroup->setVisible(local);
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
        m_modelEdit->text(),
        m_languageEdit->text(),
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
        draft.language = m_languageEdit->text();
        draft.prompt = m_promptEdit->toPlainText();
        if (backendLabel == QLatin1String("Local STT")) {
            draft.apiUrl = QString::fromLatin1(LocalUrl);
            draft.model = m_modelEdit->text();
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
    m_languageEdit->setText(draft.language);
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
    const QString name = selectedModelName();
    const auto answer = QMessageBox::question(
        this,
        QStringLiteral("Delete local model"),
        QStringLiteral("Delete the downloaded model “%1”?\n\nOnly its catalog-managed default GGUF will be removed.").arg(name),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (answer == QMessageBox::Yes && !m_modelManager->startDelete(selectedModelId())) {
        m_modelStatusLabel->setText(QStringLiteral("Could not start model deletion."));
    }
}

void SettingsDialog::modelOperationStarted(const QString &operation, const QString &modelId)
{
    m_modelOperationBusy = true;
    const auto model = m_catalog.modelById(modelId);
    const QString name = model ? model->name : modelId;
    m_modelStatusLabel->setText(operation == QLatin1String("delete")
                                    ? QStringLiteral("Deleting %1…").arg(name)
                                    : QStringLiteral("Downloading %1…").arg(name));
    updateModelControls();
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
    const auto model = m_catalog.modelById(modelId);
    const QString name = model ? model->name : modelId;
    if (success) {
        if (operation == QLatin1String("download")) {
            m_installedModelIds.insert(modelId);
            m_modelStatusLabel->setText(QStringLiteral("Downloaded %1.").arg(name));
        } else if (operation == QLatin1String("delete")) {
            m_installedModelIds.remove(modelId);
            m_modelStatusLabel->setText(QStringLiteral("Deleted %1.").arg(name));
        }
        populateModels(modelId);
    } else {
        const QString action = operation == QLatin1String("delete") ? QStringLiteral("Delete") : QStringLiteral("Download");
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
    settings.apiKey = m_apiKeyEdit->text();
    settings.model = local ? selectedModelId() : m_modelEdit->text().trimmed();
    settings.language = m_languageEdit->text().trimmed();
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
    if (settings.apiUrl == QLatin1String(LocalUrl)) {
        return QStringLiteral("Local STT");
    }
    return QStringLiteral("OpenAI");
}
