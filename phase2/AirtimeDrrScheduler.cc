#include "AirtimeDrrScheduler.h"

namespace ca2 {

Define_Module(AirtimeDrrScheduler);

void AirtimeDrrScheduler::initialize(int stage)
{
    AirtimeSchedulerBase::initialize(stage);
    if (stage == INITSTAGE_LOCAL) {
        quantum = par("quantum");
        if (quantum <= SIMTIME_ZERO)
            throw cRuntimeError("Airtime quantum must be positive");
        deficit.assign(getNumQueues(), SIMTIME_ZERO);
        WATCH_VECTOR(deficit);
        WATCH(current);
    }
}

// Called once each time the MAC takes a frame. Return the index of the queue
// to serve, or -1 if every queue is empty. Available helpers (see
// AirtimeSchedulerBase.h): getNumQueues(), hasPacket(i), headAirtime(i).
int AirtimeDrrScheduler::schedulePacket()
{
    const int n = getNumQueues();
    bool anyPackets = false;
    for (int i = 0; i < n; ++i) {
        if (hasPacket(i))
            anyPackets = true;
        else
            deficit[i] = SIMTIME_ZERO;
    }
    if (!anyPackets)
        return -1;

    while (true) {
        if (hasPacket(current)) {
            auto& credit = deficit[current];
            if (credit > SIMTIME_ZERO) {
                credit -= headAirtime(current);
                const int selected = current;
                // The MAC immediately pulls this frame. Clear credit if it drains the queue.
                if (collections[selected]->getNumPackets() == 1) {
                    credit = SIMTIME_ZERO;
                    current = (current + 1) % n;
                }
                return selected;
            }
            credit += quantum;
        }
        current = (current + 1) % n;
    }
}

} // namespace ca2
