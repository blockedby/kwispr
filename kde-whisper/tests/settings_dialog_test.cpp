#include "ui/SettingsDialog.h"

#include <QtTest/QtTest>
#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSignalSpy>

class SettingsDialogTest : public QObject {
    Q_OBJECT
private slots:
    void loadsCurrentSettingsIntoWidgets();
    void backendPresetChangesDoNotSaveUntilApply();
    void apiKeyIsPasswordAndValidationDoesNotLeakSecret();
    void saveWritesThroughEnvFileWhenValid();
    void modelComboShowsNotInstalledModels();
    void localModelComboSelectsActiveModelAndUpdatesModelField();
    void importantFieldsHaveHelpTooltips();
};

static ModelCatalog sampleCatalog()
{
    ModelCatalog catalog;
    catalog.isValid = true;
    catalog.models.append(LocalModel{QStringLiteral("whisper-large-v3-turbo"), QStringLiteral("Large v3 Turbo"), QStringLiteral("whisper.cpp"), false, {QStringLiteral("en"), QStringLiteral("ru")}, true});
    catalog.models.append(LocalModel{QStringLiteral("parakeet-tdt"), QStringLiteral("Parakeet TDT"), QStringLiteral("parakeet"), true, {}, false});
    return catalog;
}

void SettingsDialogTest::loadsCurrentSettingsIntoWidgets()
{
    KwisprSettings settings;
    settings.backend = QStringLiteral("openrouter-chat");
    settings.apiUrl = QStringLiteral("https://openrouter.ai/api/v1/chat/completions");
    settings.apiKey = QStringLiteral("secret-key");
    settings.model = QStringLiteral("openai/gpt-4o-mini-transcribe");
    settings.language = QStringLiteral("ru");
    settings.transcriptionPrompt = QStringLiteral("Clean punctuation only");
    settings.autopaste = false;
    settings.pasteHotkey = QStringLiteral("ctrl-shift-v");
    settings.autopasteDelay = 0.75;
    settings.vadEnabled = true;
    settings.vadProvider = QStringLiteral("silero");
    settings.vadModelPath = QStringLiteral("/models/silero.onnx");

    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")});

    QCOMPARE(dialog.findChild<QComboBox *>("backendCombo")->currentText(), QStringLiteral("OpenRouter"));
    QCOMPARE(dialog.findChild<QLineEdit *>("apiUrlEdit")->text(), settings.apiUrl);
    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->text(), settings.apiKey);
    QCOMPARE(dialog.findChild<QLineEdit *>("modelEdit")->text(), settings.model);
    QCOMPARE(dialog.findChild<QLineEdit *>("languageEdit")->text(), settings.language);
    QCOMPARE(dialog.findChild<QPlainTextEdit *>("promptEdit")->toPlainText(), settings.transcriptionPrompt);
    QVERIFY(!dialog.findChild<QCheckBox *>("autopasteCheck")->isChecked());
    QCOMPARE(dialog.findChild<QComboBox *>("pasteHotkeyCombo")->currentText(), settings.pasteHotkey);
    QCOMPARE(dialog.findChild<QDoubleSpinBox *>("autopasteDelaySpin")->value(), settings.autopasteDelay);
    QVERIFY(dialog.findChild<QCheckBox *>("vadEnabledCheck")->isChecked());
}

void SettingsDialogTest::backendPresetChangesDoNotSaveUntilApply()
{
    KwisprSettings settings;
    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")});
    QSignalSpy savedSpy(&dialog, &SettingsDialog::settingsSaved);

    auto *backend = dialog.findChild<QComboBox *>("backendCombo");
    backend->setCurrentText(QStringLiteral("Local STT"));

    QCOMPARE(dialog.findChild<QLineEdit *>("apiUrlEdit")->text(), QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));
    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->text(), QString());
    QCOMPARE(savedSpy.count(), 0);
}

void SettingsDialogTest::apiKeyIsPasswordAndValidationDoesNotLeakSecret()
{
    KwisprSettings settings;
    settings.apiUrl = QStringLiteral("https://api.openai.com/v1/audio/transcriptions");
    settings.apiKey = QStringLiteral("super-secret-token");
    settings.pasteHotkey = QStringLiteral("bad-hotkey");
    SettingsDialog dialog(settings, sampleCatalog(), {});

    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->echoMode(), QLineEdit::Password);
    QVERIFY(!dialog.save());
    QVERIFY(!dialog.lastError().contains(QStringLiteral("super-secret-token")));
    QVERIFY(dialog.lastError().contains(QStringLiteral("Unsupported paste hotkey")));
}

void SettingsDialogTest::saveWritesThroughEnvFileWhenValid()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("whisper-large-v3-turbo"), QStringLiteral("/tmp/models"), QString());
    EnvFile env;
    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, &env);

    dialog.findChild<QLineEdit *>("languageEdit")->setText(QStringLiteral("ru"));
    dialog.findChild<QPlainTextEdit *>("promptEdit")->setPlainText(QStringLiteral("Keep slang"));
    dialog.findChild<QCheckBox *>("autopasteCheck")->setChecked(true);

    QVERIFY(dialog.save());
    QCOMPARE(env.value("KWISPR_API_URL"), QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));
    QCOMPARE(env.value("KWISPR_MODEL"), QStringLiteral("whisper-large-v3-turbo"));
    QCOMPARE(env.value("KWISPR_LANGUAGE"), QStringLiteral("ru"));
    QCOMPARE(env.value("KWISPR_TRANSCRIPTION_PROMPT"), QStringLiteral("Keep slang"));
    QCOMPARE(env.value("KWISPR_AUTOPASTE"), QStringLiteral("1"));
}

void SettingsDialogTest::localModelComboSelectsActiveModelAndUpdatesModelField()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("parakeet-tdt"), QString(), QString());
    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("parakeet-tdt")});

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    auto *modelEdit = dialog.findChild<QLineEdit *>("modelEdit");
    QCOMPARE(models->currentData().toString(), QStringLiteral("parakeet-tdt"));
    QCOMPARE(modelEdit->text(), QStringLiteral("parakeet-tdt"));

    dialog.findChild<QComboBox *>("backendCombo")->setCurrentText(QStringLiteral("Local STT"));
    models->setCurrentIndex(0);
    QCOMPARE(modelEdit->text(), QStringLiteral("whisper-large-v3-turbo"));
}

void SettingsDialogTest::importantFieldsHaveHelpTooltips()
{
    KwisprSettings settings;
    SettingsDialog dialog(settings, sampleCatalog(), {});

    QVERIFY(!dialog.findChild<QComboBox *>("backendCombo")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QLineEdit *>("apiUrlEdit")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QLineEdit *>("modelEdit")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QComboBox *>("localModelCombo")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QLineEdit *>("languageEdit")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QPlainTextEdit *>("promptEdit")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QDoubleSpinBox *>("autopasteDelaySpin")->toolTip().isEmpty());
    QVERIFY(!dialog.findChild<QDoubleSpinBox *>("vadThresholdSpin")->toolTip().isEmpty());
}

void SettingsDialogTest::modelComboShowsNotInstalledModels()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("whisper-large-v3-turbo"), QString(), QString());
    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")});

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    QCOMPARE(models->count(), 2);
    QCOMPARE(models->itemText(0), QStringLiteral("Large v3 Turbo (installed)"));
    QCOMPARE(models->itemData(0).toString(), QStringLiteral("whisper-large-v3-turbo"));
    QCOMPARE(models->itemText(1), QStringLiteral("Parakeet TDT (not installed)"));
    QCOMPARE(models->itemData(1).toString(), QStringLiteral("parakeet-tdt"));
}

QTEST_MAIN(SettingsDialogTest)
#include "settings_dialog_test.moc"
