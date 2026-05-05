#include "ui/SettingsDialog.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
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
}

SettingsDialog::SettingsDialog(const KwisprSettings &settings,
                               const ModelCatalog &catalog,
                               const QStringList &installedModelIds,
                               EnvFile *env,
                               QWidget *parent)
    : QDialog(parent)
    , m_catalog(catalog)
    , m_installedModelIds(installedModelIds.begin(), installedModelIds.end())
    , m_env(env)
    , m_settings(settings)
{
    buildUi();
    populateModels();
    loadFromSettings(settings);
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

void SettingsDialog::buildUi()
{
    setWindowTitle(QStringLiteral("KDE Whisper Settings"));
    auto *root = new QVBoxLayout(this);

    auto *backendGroup = new QGroupBox(QStringLiteral("Backend"), this);
    auto *backendForm = new QFormLayout(backendGroup);

    m_backendCombo = new QComboBox(backendGroup);
    m_backendCombo->setObjectName(QStringLiteral("backendCombo"));
    m_backendCombo->addItems({QStringLiteral("Local STT"), QStringLiteral("OpenAI"), QStringLiteral("OpenRouter")});
    m_backendCombo->setToolTip(QStringLiteral("Choose Local STT for downloaded on-device models, OpenAI for Whisper API, or OpenRouter for chat-based transcription."));
    backendForm->addRow(QStringLiteral("Backend"), m_backendCombo);

    m_apiUrlEdit = new QLineEdit(backendGroup);
    m_apiUrlEdit->setObjectName(QStringLiteral("apiUrlEdit"));
    m_apiUrlEdit->setToolTip(QStringLiteral("Endpoint used for transcription. Presets fill this automatically; edit only for a compatible custom service."));
    backendForm->addRow(QStringLiteral("API URL"), m_apiUrlEdit);

    m_apiKeyEdit = new QLineEdit(backendGroup);
    m_apiKeyEdit->setObjectName(QStringLiteral("apiKeyEdit"));
    m_apiKeyEdit->setEchoMode(QLineEdit::Password);
    m_apiKeyEdit->setToolTip(QStringLiteral("Required for hosted OpenAI/OpenRouter backends. Leave empty for Local STT."));
    backendForm->addRow(QStringLiteral("API key"), m_apiKeyEdit);

    m_modelEdit = new QLineEdit(backendGroup);
    m_modelEdit->setObjectName(QStringLiteral("modelEdit"));
    m_modelEdit->setToolTip(QStringLiteral("Model identifier sent to the selected backend. For Local STT, use an installed model from the list below."));
    backendForm->addRow(QStringLiteral("Model"), m_modelEdit);

    m_localModelCombo = new QComboBox(backendGroup);
    m_localModelCombo->setObjectName(QStringLiteral("localModelCombo"));
    m_localModelCombo->setToolTip(QStringLiteral("Local models from the catalog. Installed status is detected from your model directory."));
    backendForm->addRow(QStringLiteral("Local model"), m_localModelCombo);

    m_languageEdit = new QLineEdit(backendGroup);
    m_languageEdit->setObjectName(QStringLiteral("languageEdit"));
    m_languageEdit->setToolTip(QStringLiteral("Optional language hint such as en or ru. Leave blank to auto-detect when supported."));
    backendForm->addRow(QStringLiteral("Language"), m_languageEdit);

    m_promptEdit = new QPlainTextEdit(backendGroup);
    m_promptEdit->setObjectName(QStringLiteral("promptEdit"));
    m_promptEdit->setMinimumHeight(80);
    m_promptEdit->setToolTip(QStringLiteral("Optional transcription guidance, especially useful for OpenRouter/chat backends."));
    backendForm->addRow(QStringLiteral("Prompt"), m_promptEdit);
    root->addWidget(backendGroup);

    auto *pasteGroup = new QGroupBox(QStringLiteral("Paste"), this);
    auto *pasteForm = new QFormLayout(pasteGroup);
    m_autopasteCheck = new QCheckBox(QStringLiteral("Paste automatically after transcription"), pasteGroup);
    m_autopasteCheck->setObjectName(QStringLiteral("autopasteCheck"));
    pasteForm->addRow(QString(), m_autopasteCheck);

    m_pasteHotkeyCombo = new QComboBox(pasteGroup);
    m_pasteHotkeyCombo->setObjectName(QStringLiteral("pasteHotkeyCombo"));
    m_pasteHotkeyCombo->setEditable(true);
    m_pasteHotkeyCombo->addItems({QStringLiteral("shift-insert"), QStringLiteral("ctrl-v"), QStringLiteral("ctrl-shift-v")});
    m_pasteHotkeyCombo->setToolTip(QStringLiteral("Keyboard shortcut Kwispr sends when auto-pasting into the active window."));
    pasteForm->addRow(QStringLiteral("Paste hotkey"), m_pasteHotkeyCombo);

    m_autopasteDelaySpin = new QDoubleSpinBox(pasteGroup);
    m_autopasteDelaySpin->setObjectName(QStringLiteral("autopasteDelaySpin"));
    m_autopasteDelaySpin->setRange(0.0, 10.0);
    m_autopasteDelaySpin->setDecimals(2);
    m_autopasteDelaySpin->setSingleStep(0.05);
    m_autopasteDelaySpin->setToolTip(QStringLiteral("Seconds to wait after transcription before sending the paste hotkey."));
    pasteForm->addRow(QStringLiteral("Paste delay"), m_autopasteDelaySpin);
    root->addWidget(pasteGroup);

    auto *vadGroup = new QGroupBox(QStringLiteral("Voice activity detection"), this);
    auto *vadForm = new QFormLayout(vadGroup);
    m_vadEnabledCheck = new QCheckBox(QStringLiteral("Enable VAD"), vadGroup);
    m_vadEnabledCheck->setObjectName(QStringLiteral("vadEnabledCheck"));
    vadForm->addRow(QString(), m_vadEnabledCheck);

    m_vadProviderCombo = new QComboBox(vadGroup);
    m_vadProviderCombo->setObjectName(QStringLiteral("vadProviderCombo"));
    m_vadProviderCombo->addItems({QStringLiteral("energy"), QStringLiteral("silero")});
    m_vadProviderCombo->setToolTip(QStringLiteral("Energy is lightweight; Silero requires a local ONNX model path."));
    vadForm->addRow(QStringLiteral("Provider"), m_vadProviderCombo);

    m_vadModelPathEdit = new QLineEdit(vadGroup);
    m_vadModelPathEdit->setObjectName(QStringLiteral("vadModelPathEdit"));
    m_vadModelPathEdit->setToolTip(QStringLiteral("Path to the Silero VAD ONNX model, required only when provider is silero."));
    vadForm->addRow(QStringLiteral("Silero model"), m_vadModelPathEdit);

    m_vadThresholdSpin = new QDoubleSpinBox(vadGroup);
    m_vadThresholdSpin->setObjectName(QStringLiteral("vadThresholdSpin"));
    m_vadThresholdSpin->setRange(0.0, 100.0);
    m_vadThresholdSpin->setDecimals(4);
    m_vadThresholdSpin->setSingleStep(0.05);
    m_vadThresholdSpin->setToolTip(QStringLiteral("Voice activity threshold. Higher values are stricter about what counts as speech."));
    vadForm->addRow(QStringLiteral("Threshold"), m_vadThresholdSpin);

    m_vadFrameMsEdit = new QLineEdit(vadGroup);
    m_vadFrameMsEdit->setObjectName(QStringLiteral("vadFrameMsEdit"));
    m_vadFrameMsEdit->setToolTip(QStringLiteral("Frame duration in milliseconds for VAD analysis; keep positive."));
    vadForm->addRow(QStringLiteral("Frame ms"), m_vadFrameMsEdit);
    root->addWidget(vadGroup);

    m_buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Apply | QDialogButtonBox::Cancel, this);
    m_buttons->setObjectName(QStringLiteral("buttonBox"));
    root->addWidget(m_buttons);

    connect(m_backendCombo, &QComboBox::currentTextChanged, this, &SettingsDialog::applyBackendPreset);
    connect(m_localModelCombo, &QComboBox::currentIndexChanged, this, [this](int index) {
        if (index >= 0 && m_backendCombo->currentText() == QLatin1String("Local STT")) {
            m_modelEdit->setText(m_localModelCombo->itemData(index).toString());
        }
    });
    connect(m_buttons->button(QDialogButtonBox::Apply), &QPushButton::clicked, this, &SettingsDialog::save);
    connect(m_buttons, &QDialogButtonBox::accepted, this, [this]() {
        if (save()) {
            accept();
        }
    });
    connect(m_buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
}

void SettingsDialog::loadFromSettings(const KwisprSettings &settings)
{
    const QSignalBlocker blocker(m_backendCombo);
    m_backendCombo->setCurrentText(backendLabelForSettings(settings));
    m_apiUrlEdit->setText(settings.apiUrl);
    m_apiKeyEdit->setText(settings.apiKey);
    m_modelEdit->setText(settings.model);
    const int localIndex = m_localModelCombo->findData(settings.model);
    if (localIndex >= 0) {
        m_localModelCombo->setCurrentIndex(localIndex);
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
}

void SettingsDialog::populateModels()
{
    m_localModelCombo->clear();
    for (const LocalModel &model : m_catalog.models) {
        const bool installed = m_installedModelIds.contains(model.id);
        const QString label = QStringLiteral("%1 (%2)").arg(model.name, installed ? QStringLiteral("installed") : QStringLiteral("not installed"));
        m_localModelCombo->addItem(label, model.id);
    }
}

void SettingsDialog::applyBackendPreset(const QString &backendLabel)
{
    if (backendLabel == QLatin1String("Local STT")) {
        m_apiUrlEdit->setText(QString::fromLatin1(LocalUrl));
        m_apiKeyEdit->clear();
        if (m_localModelCombo->currentIndex() >= 0) {
            m_modelEdit->setText(m_localModelCombo->currentData().toString());
        }
    } else if (backendLabel == QLatin1String("OpenAI")) {
        m_apiUrlEdit->setText(QString::fromLatin1(OpenAiUrl));
        if (m_modelEdit->text().trimmed().isEmpty() || m_modelEdit->text().contains(QStringLiteral("whisper-large"))) {
            m_modelEdit->setText(QStringLiteral("whisper-1"));
        }
    } else if (backendLabel == QLatin1String("OpenRouter")) {
        m_apiUrlEdit->setText(QString::fromLatin1(OpenRouterUrl));
        if (m_modelEdit->text().trimmed().isEmpty() || m_modelEdit->text() == QLatin1String("whisper-1")) {
            m_modelEdit->setText(QStringLiteral("openai/gpt-4o-mini-transcribe"));
        }
    }
}

KwisprSettings SettingsDialog::settingsFromWidgets() const
{
    KwisprSettings settings = m_settings;
    settings.backend = backendValueForLabel(m_backendCombo->currentText());
    settings.apiUrl = m_apiUrlEdit->text().trimmed();
    settings.apiKey = m_apiKeyEdit->text();
    settings.model = m_modelEdit->text().trimmed();
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
