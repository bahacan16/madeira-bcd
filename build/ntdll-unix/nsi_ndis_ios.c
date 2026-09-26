/* iOS SDK omits these optional BSD headers; the interface/address APIs
 * and routing sysctl ABI are available. Reuse Wine's provider unchanged. */
#include "config.h"
#undef HAVE_NET_IF_ARP_H
#undef HAVE_NETINET_IF_ETHER_H
#undef HAVE_NETINET_IP_VAR_H
#undef HAVE_NETINET_ICMP_VAR_H
/* madeira-bcd: the iPhoneOS SDK has no net/route.h, so configure leaves
 * this undefined and the routing-socket code (RTM_IFINFO, rt_msghdr) does
 * not compile; build/ntdll-unix/shims/net/route.h carries Apple's header. */
#ifndef HAVE_NET_ROUTE_H
#define HAVE_NET_ROUTE_H 1
#endif
#include "../../wine/dlls/nsiproxy.sys/ndis.c"
