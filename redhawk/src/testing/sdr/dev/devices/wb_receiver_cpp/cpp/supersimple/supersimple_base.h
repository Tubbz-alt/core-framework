#ifndef SUPERSIMPLE_BASE_IMPL_BASE_H
#define SUPERSIMPLE_BASE_IMPL_BASE_H

#include <boost/thread.hpp>
#include <ossie/Device_impl.h>
#include <ossie/ThreadedComponent.h>
#include <ossie/DynamicComponent.h>

#define BOOL_VALUE_HERE 0

class supersimple_base : public Device_impl, protected ThreadedComponent, public virtual DynamicComponent
{
    public:
        supersimple_base(char *devMgr_ior, char *id, char *lbl, char *sftwrPrfl);
        supersimple_base(char *devMgr_ior, char *id, char *lbl, char *sftwrPrfl, char *compDev);
        supersimple_base(char *devMgr_ior, char *id, char *lbl, char *sftwrPrfl, CF::Properties capacities);
        supersimple_base(char *devMgr_ior, char *id, char *lbl, char *sftwrPrfl, CF::Properties capacities, char *compDev);
        ~supersimple_base();

        void start() throw (CF::Resource::StartError, CORBA::SystemException);

        void stop() throw (CF::Resource::StopError, CORBA::SystemException);

        void releaseObject() throw (CF::LifeCycle::ReleaseError, CORBA::SystemException);

        void loadProperties();
        void removeAllocationIdRouting(const size_t tuner_id);

    protected:
        // Member variables exposed as properties
        /// Property: device_kind
        std::string device_kind;
        /// Property: device_model
        std::string device_model;
        /// Property: abc
        std::string abc;

    private:
        void construct();
};
#endif // SUPERSIMPLE_BASE_IMPL_BASE_H
