#include "ActivationRouting.h"

namespace ActivationRouting {
Action actionForArguments(const QStringList &arguments)
{
    return arguments.contains(QStringLiteral("--settings")) ? Action::OpenSettings : Action::None;
}
}
