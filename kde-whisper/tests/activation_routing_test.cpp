#include "ActivationRouting.h"

#include <QtTest/QtTest>

class ActivationRoutingTest : public QObject
{
    Q_OBJECT
private slots:
    void settingsArgumentOpensSettings();
    void ordinaryActivationDoesNothing();
};

void ActivationRoutingTest::settingsArgumentOpensSettings()
{
    QCOMPARE(ActivationRouting::actionForArguments(
                 {QStringLiteral("kde-whisper"), QStringLiteral("--settings")}),
             ActivationRouting::Action::OpenSettings);
}

void ActivationRoutingTest::ordinaryActivationDoesNothing()
{
    QCOMPARE(ActivationRouting::actionForArguments({}), ActivationRouting::Action::None);
    QCOMPARE(ActivationRouting::actionForArguments({QStringLiteral("kde-whisper")}),
             ActivationRouting::Action::None);
}

QTEST_APPLESS_MAIN(ActivationRoutingTest)
#include "activation_routing_test.moc"
