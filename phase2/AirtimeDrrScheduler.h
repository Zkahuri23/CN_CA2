#ifndef __CA2_AIRTIMEDRRSCHEDULER_H
#define __CA2_AIRTIMEDRRSCHEDULER_H

#include "AirtimeSchedulerBase.h"

namespace ca2 {

/**
 * Airtime deficit round robin at the AP. You may add members here.
 */
class AirtimeDrrScheduler : public AirtimeSchedulerBase
{
  protected:
    simtime_t quantum;
    std::vector<simtime_t> deficit;     // positive credit or airtime debt, per queue
    int current = 0;                    // queue the round robin is at

    virtual void initialize(int stage) override;
    virtual int schedulePacket() override;
};

} // namespace ca2

#endif
