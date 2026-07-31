#pragma once

#include <QStringList>

namespace ActivationRouting {
enum class Action {
    None,
    OpenSettings,
};

Action actionForArguments(const QStringList &arguments);
}
