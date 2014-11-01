#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr

from neutron.api.rpc.agentnotifiers import dhcp_rpc_agent_api
from neutron.api.v2 import attributes
from neutron.common import constants as const
from neutron.common import exceptions as n_exc
from neutron.common import log
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import securitygroup as ext_sg
from neutron import manager
from neutron.notifiers import nova
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as pconst
from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy.orm import exc as sql_exc

from gbp.neutron.db.grouppolicy import group_policy_db as gpdb
from gbp.neutron.services.grouppolicy.common import constants as gconst
from gbp.neutron.services.grouppolicy.common import exceptions as exc
from gbp.neutron.services.grouppolicy import group_policy_driver_api as api


LOG = logging.getLogger(__name__)


class OwnedPort(model_base.BASEV2):
    """A Port owned by the resource_mapping driver."""

    __tablename__ = 'gpm_owned_ports'
    port_id = sa.Column(sa.String(36),
                        sa.ForeignKey('ports.id', ondelete='CASCADE'),
                        nullable=False, primary_key=True)


class OwnedSubnet(model_base.BASEV2):
    """A Subnet owned by the resource_mapping driver."""

    __tablename__ = 'gpm_owned_subnets'
    subnet_id = sa.Column(sa.String(36),
                          sa.ForeignKey('subnets.id', ondelete='CASCADE'),
                          nullable=False, primary_key=True)


class OwnedNetwork(model_base.BASEV2):
    """A Network owned by the resource_mapping driver."""

    __tablename__ = 'gpm_owned_networks'
    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id', ondelete='CASCADE'),
                           nullable=False, primary_key=True)


class OwnedRouter(model_base.BASEV2):
    """A Router owned by the resource_mapping driver."""

    __tablename__ = 'gpm_owned_routers'
    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey('routers.id', ondelete='CASCADE'),
                          nullable=False, primary_key=True)


class ContractSGsMapping(model_base.BASEV2):
    """Contract to SGs mapping DB."""

    __tablename__ = 'gpm_contract_sg_mapping'
    contract_id = sa.Column(sa.String(36),
                            sa.ForeignKey('gp_contracts.id',
                                          ondelete='CASCADE'),
                            nullable=False, primary_key=True)
    provided_sg_id = sa.Column(sa.String(36),
                               sa.ForeignKey('securitygroups.id'))
    consumed_sg_id = sa.Column(sa.String(36),
                               sa.ForeignKey('securitygroups.id'))


class RuleServiceChainInstanceMapping(model_base.BASEV2):
    """Policy Rule to ServiceChainInstance mapping DB."""

    __tablename__ = 'gpm_rule_servicechain_mapping'
    rule_id = sa.Column(sa.String(36),
                        sa.ForeignKey('gp_policy_rules.id'),
                        nullable=False, primary_key=True)
    servicechain_instance_id = sa.Column(sa.String(36),
                                         sa.ForeignKey('sc_instances.id',
                                                       ondelete='CASCADE'))


class ServicePolicyEPGIpAddressMapping(model_base.BASEV2):
    """Service Policy to IP Address mapping DB."""

    __tablename__ = 'gpm_service_policy_ipaddress_mappings'
    service_policy_id = sa.Column(sa.String(36),
                        sa.ForeignKey('gp_network_service_policies.id'),
                        nullable=False, primary_key=True)
    endpoint_group = sa.Column(sa.String(36),
                        sa.ForeignKey('gp_network_service_policies.id'),
                        nullable=False, primary_key=True)
    ipaddress = sa.Column(sa.String(36))


class ResourceMappingDriver(api.PolicyDriver):
    """Resource Mapping driver for Group Policy plugin.

    This driver implements group policy semantics by mapping group
    policy resources to various other neutron resources.
    """

    @log.log
    def initialize(self):
        self._cached_agent_notifier = None
        self._nova_notifier = nova.Notifier()

    @log.log
    def create_endpoint_precommit(self, context):
        if not context.current['endpoint_group_id']:
            raise exc.EndpointRequiresEndpointGroup()

    @log.log
    def create_endpoint_postcommit(self, context):
        # TODO(rkukura): Validate explicit port belongs to subnet of
        # EPG.
        if not context.current['port_id']:
            self._use_implicit_port(context)
        self._assoc_epg_sg_to_ep(context, context.current['id'],
                                 context.current['endpoint_group_id'])

    @log.log
    def update_endpoint_precommit(self, context):
        if (context.current['endpoint_group_id'] !=
            context.original['endpoint_group_id']):
            raise exc.EndpointEndpointGroupUpdateNotSupported()

    @log.log
    def update_endpoint_postcommit(self, context):
        pass

    @log.log
    def delete_endpoint_precommit(self, context):
        pass

    @log.log
    def delete_endpoint_postcommit(self, context):
        sg_list = self._generate_list_of_sg_from_epg(
            context, context.current['endpoint_group_id'])
        self._disassoc_sgs_from_port(context._plugin_context,
                                     context.current['port_id'], sg_list)
        port_id = context.current['port_id']
        self._cleanup_port(context._plugin_context, port_id)

    @log.log
    def create_endpoint_group_precommit(self, context):
        pass

    @log.log
    def create_endpoint_group_postcommit(self, context):
        # TODO(rkukura): Validate explicit subnet belongs to L2P's
        # network.
        subnets = context.current['subnets']
        if subnets:
            l2p_id = context.current['l2_policy_id']
            l2p = context._plugin.get_l2_policy(context._plugin_context,
                                                l2p_id)
            l3p_id = l2p['l3_policy_id']
            l3p = context._plugin.get_l3_policy(context._plugin_context,
                                                l3p_id)
            router_id = l3p['routers'][0] if l3p['routers'] else None
            for subnet_id in subnets:
                self._use_explicit_subnet(context._plugin_context, subnet_id,
                                          router_id)
        else:
            self._use_implicit_subnet(context)
        self._handle_network_service_policy(context)
        self._handle_contracts(context)

    def _handle_network_service_policy(self, context):
        network_service_policy_id = context.current.get(
                                            "network_service_policy_id")
        if not network_service_policy_id:
            return

        nsp = context._plugin.get_network_service_policy(
                            context._plugin_context, network_service_policy_id)
        nsp_params = nsp.get("network_service_params")
        if not nsp_params:
            return

        #RM Driver only supports one parameter of type ip_single and value
        #self_subnet right now. Handle the other cases when we have usecase
        if (len(nsp_params) > 1 or nsp_params[0].get("type") != "ip_single"
            or nsp_params[0].get("value") != "self_subnet"):
            return
        #TODO(Magesh):Handle concurrency issues
        free_ip = self._get_last_free_ip(context._plugin_context,
                                         context.current['subnets'])
        if not free_ip:
            LOG.error(_("Reserving IP Addresses failed for Network Service "
                        "Policy. No more IP Addresses on subnet"))
            return
        #TODO(Magesh):Fetch subnet from EPG to which Service Policy is attached
        self._remove_ip_from_allocation_pool(context,
                                             context.current['subnets'][0],
                                             free_ip)
        self._set_policy_ipaddress_mapping(context._plugin_context.session,
                                           network_service_policy_id,
                                           context.current['id'],
                                           free_ip)

    def _get_service_policy_ipaddress(self, context, endpoint_group):
        ipaddress = self._get_epg_policy_ipaddress_mapping(
                                            context._plugin_context.session,
                                            endpoint_group)
        return ipaddress

    def _cleanup_network_service_policy(self, context, subnet, epg_id):
        ipaddress = self._get_epg_policy_ipaddress_mapping(
                                            context._plugin_context.session,
                                            epg_id)
        if ipaddress:
            self._restore_ip_to_allocation_pool(context, subnet, ipaddress)
            self._delete_policy_ipaddress_mapping(
                        context._plugin_context.session, epg_id)

    @log.log
    def update_endpoint_group_precommit(self, context):
        if set(context.original['subnets']) - set(context.current['subnets']):
            raise exc.EndpointGroupSubnetRemovalNotSupported()

    @log.log
    def update_endpoint_group_postcommit(self, context):
        # Three conditions where SG association needs to be changed
        # (a) list of endpoints change
        # (b) provided_contracts change
        # (c) consumed_contracts change
        epg_id = context.current['id']
        new_endpoints = list(set(context.current['endpoints']) -
                             set(context.original['endpoints']))
        if new_endpoints:
            self._update_sgs_on_ep_with_epg(context, epg_id,
                                            new_endpoints, "ASSOCIATE")
        removed_endpoints = list(set(context.original['endpoints']) -
                                 set(context.current['endpoints']))
        if removed_endpoints:
            self._update_sgs_on_ep_with_epg(context, epg_id,
                                            new_endpoints, "DISASSOCIATE")
        # generate a list of contracts (SGs) to update on the EPG
        orig_provided_contracts = context.original['provided_contracts']
        curr_provided_contracts = context.current['provided_contracts']
        new_provided_contracts = list(set(curr_provided_contracts) -
                                      set(orig_provided_contracts))
        orig_consumed_contracts = context.original['consumed_contracts']
        curr_consumed_contracts = context.current['consumed_contracts']
        new_consumed_contracts = list(set(curr_consumed_contracts) -
                                      set(orig_consumed_contracts))
        # if EPG associated contracts are updated, we need to update
        # the policy rules, then assoicate SGs to ports
        if new_provided_contracts or new_consumed_contracts:
            subnets = context.current['subnets']
            self._assoc_sg_to_epg(context, subnets, new_provided_contracts,
                                  new_consumed_contracts)
            self._update_sgs_on_epg(context, epg_id,
                                    new_provided_contracts,
                                    new_consumed_contracts, "ASSOCIATE")
        # generate the list of contracts (SGs) to remove from current ports
        removed_provided_contracts = list(set(orig_provided_contracts) -
                                          set(curr_provided_contracts))
        removed_consumed_contracts = list(set(orig_consumed_contracts) -
                                          set(curr_consumed_contracts))
        if removed_provided_contracts or removed_consumed_contracts:
            self._update_sgs_on_epg(context, epg_id,
                                    removed_provided_contracts,
                                    removed_consumed_contracts, "DISASSOCIATE")

    @log.log
    def delete_endpoint_group_precommit(self, context):
        pass

    @log.log
    def delete_endpoint_group_postcommit(self, context):
        self._cleanup_network_service_policy(context,
                                             context.current['subnets'][0],
                                             context.current['id'])
        self._cleanup_redirect_action(context)
        l2p_id = context.current['l2_policy_id']
        router_id = self._get_routerid_for_l2policy(context, l2p_id)
        for subnet_id in context.current['subnets']:
            self._cleanup_subnet(context._plugin_context, subnet_id, router_id)

    @log.log
    def create_l2_policy_precommit(self, context):
        pass

    @log.log
    def create_l2_policy_postcommit(self, context):
        if not context.current['network_id']:
            self._use_implicit_network(context)

    @log.log
    def update_l2_policy_precommit(self, context):
        pass

    @log.log
    def update_l2_policy_postcommit(self, context):
        pass

    @log.log
    def delete_l2_policy_precommit(self, context):
        pass

    @log.log
    def delete_l2_policy_postcommit(self, context):
        network_id = context.current['network_id']
        self._cleanup_network(context._plugin_context, network_id)

    @log.log
    def create_l3_policy_precommit(self, context):
        if len(context.current['routers']) > 1:
            raise exc.L3PolicyMultipleRoutersNotSupported()

    @log.log
    def create_l3_policy_postcommit(self, context):
        if not context.current['routers']:
            self._use_implicit_router(context)

    @log.log
    def update_l3_policy_precommit(self, context):
        if context.current['routers'] != context.original['routers']:
            raise exc.L3PolicyRoutersUpdateNotSupported()

    @log.log
    def update_l3_policy_postcommit(self, context):
        pass

    @log.log
    def delete_l3_policy_precommit(self, context):
        pass

    @log.log
    def delete_l3_policy_postcommit(self, context):
        for router_id in context.current['routers']:
            self._cleanup_router(context._plugin_context, router_id)

    @log.log
    def create_policy_classifier_precommit(self, context):
        pass

    @log.log
    def create_policy_classifier_postcommit(self, context):
        pass

    @log.log
    def update_policy_classifier_precommit(self, context):
        pass

    @log.log
    def update_policy_classifier_postcommit(self, context):
        # TODO(ivar): Should affect related SGs
        pass

    @log.log
    def delete_policy_classifier_precommit(self, context):
        pass

    @log.log
    def delete_policy_classifier_postcommit(self, context):
        pass

    @log.log
    def create_policy_action_precommit(self, context):
        pass

    @log.log
    def create_policy_action_postcommit(self, context):
        pass

    @log.log
    def update_policy_action_precommit(self, context):
        pass

    @log.log
    def update_policy_action_postcommit(self, context):
        # TODO(ivar): Should affect related SGs
        pass

    @log.log
    def delete_policy_action_precommit(self, context):
        pass

    @log.log
    def delete_policy_action_postcommit(self, context):
        pass

    @log.log
    def create_policy_rule_precommit(self, context):
        pass

    @log.log
    def create_policy_rule_postcommit(self, context):
        pass

    @log.log
    def update_policy_rule_precommit(self, context):
        pass

    @log.log
    def update_policy_rule_postcommit(self, context):
        # TODO(ivar): Should affect related SGs
        pass

    @log.log
    def delete_policy_rule_precommit(self, context):
        # REVISIT(ivar): This will be removed once navigability issue is
        # solved (bug/1384397)
        context._rmd_contracts_temp = (
            context._plugin._get_policy_rule_contracts(context._plugin_context,
                                                       context.current['id']))

    @log.log
    def delete_policy_rule_postcommit(self, context):
        for contract_id in context._rmd_contracts_temp:
            contract = context._plugin.get_contract(
                context._plugin_context, contract_id)
            contract_sg_mappings = self._get_contract_sg_mapping(
                context._plugin_context.session, contract['id'])
            epg_mapping = self._get_contract_epg_mapping(context, contract)
            cidr_mapping = self._get_epg_cidrs_mapping(context, epg_mapping)
            self._add_or_remove_contract_rule(context, context.current,
                                              contract_sg_mappings,
                                              cidr_mapping,
                                              unset=True)

    @log.log
    def create_contract_precommit(self, context):
        pass

    @log.log
    def create_contract_postcommit(self, context):
        # creating SGs
        contract_id = context.current['id']
        consumed_sg = self._create_contract_sg(context, 'consumed')
        provided_sg = self._create_contract_sg(context, 'provided')
        consumed_sg_id = consumed_sg['id']
        provided_sg_id = provided_sg['id']
        self._set_contract_sg_mapping(context._plugin_context.session,
                                      contract_id, consumed_sg_id,
                                      provided_sg_id)
        self._apply_contract_rules(context, context.current,
                                   context.current['policy_rules'])

    @log.log
    def update_contract_precommit(self, context):
        pass

    @log.log
    def update_contract_postcommit(self, context):
        # Update contract rules
        old_rules = set(context.original['policy_rules'])
        new_rules = set(context.current['policy_rules'])
        to_add = new_rules - old_rules
        to_remove = old_rules - new_rules
        self._remove_contract_rules(context, context.current, to_remove)
        self._apply_contract_rules(context, context.current, to_add)
        # Update children contraint
        to_recompute = (set(context.original['child_contracts']) ^
                        set(context.current['child_contracts']))
        self._recompute_contracts(context, to_recompute)
        if to_add or to_remove:
            to_recompute = (set(context.original['child_contracts']) &
                            set(context.current['child_contracts']))
            self._recompute_contracts(context, to_recompute)

    @log.log
    def delete_contract_precommit(self, context):
        mapping = self._get_contract_sg_mapping(
            context._plugin_context.session, context.current['id'])
        context._rmd_sg_list_temp = [mapping['provided_sg_id'],
                                     mapping['consumed_sg_id']]
        contract = context._plugin._get_contract(
                 context._plugin_context, context.current['id'])
        context._rmd_epg_mapping_temp = self._get_contract_epg_mapping(
             context, contract)

    @log.log
    def delete_contract_postcommit(self, context):
        # Disassociate SGs
        sg_list = context._rmd_sg_list_temp

        epg_mapping = context._rmd_epg_mapping_temp
        for epgs in epg_mapping.values():
            for epg in epgs:
                endpoint_list = epg['endpoints']
                for ep_id in endpoint_list:
                    self._disassoc_sgs_from_ep(context, ep_id, sg_list)
        # Delete SGs
        for sg in sg_list:
            self._delete_sg(context._plugin_context, sg)

    @log.log
    def delete_network_service_policy_postcommit(self, context):
        for epg_id in context.current.get("endpoint_groups"):
            epg = context._plugin.get_endpoint_group(context._plugin_context,
                                                     epg_id)
            subnet = epg.get('subnets')[0]
            self._cleanup_network_service_policy(self, context, subnet, epg_id)

    def _get_routerid_for_l2policy(self, context, l2p_id):
        l2p = context._plugin.get_l2_policy(context._plugin_context, l2p_id)
        l3p_id = l2p['l3_policy_id']
        l3p = context._plugin.get_l3_policy(context._plugin_context, l3p_id)
        return l3p['routers'][0]

    def _use_implicit_port(self, context):
        epg_id = context.current['endpoint_group_id']
        epg = context._plugin.get_endpoint_group(context._plugin_context,
                                                 epg_id)
        l2p_id = epg['l2_policy_id']
        l2p = context._plugin.get_l2_policy(context._plugin_context, l2p_id)
        sg_id = self._ensure_default_security_group(
            context._plugin_context, context.current['tenant_id'])
        attrs = {'tenant_id': context.current['tenant_id'],
                 'name': 'ep_' + context.current['name'],
                 'network_id': l2p['network_id'],
                 'mac_address': attributes.ATTR_NOT_SPECIFIED,
                 'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                 'device_id': '',
                 'device_owner': '',
                 'security_groups': [sg_id] if sg_id else None,
                 'admin_state_up': True}
        port = self._create_port(context._plugin_context, attrs)
        port_id = port['id']
        self._mark_port_owned(context._plugin_context.session, port_id)
        context.set_port_id(port_id)

    def _cleanup_port(self, plugin_context, port_id):
        if self._port_is_owned(plugin_context.session, port_id):
            self._delete_port(plugin_context, port_id)

    def _use_implicit_subnet(self, context):
        # REVISIT(rkukura): This is a temporary allocation algorithm
        # that depends on an exception being raised when the subnet
        # being created is already in use. A DB allocation table for
        # the pool of subnets, or at least a more efficient way to
        # test if a subnet is in-use, may be needed.
        l2p_id = context.current['l2_policy_id']
        l2p = context._plugin.get_l2_policy(context._plugin_context, l2p_id)
        l3p_id = l2p['l3_policy_id']
        l3p = context._plugin.get_l3_policy(context._plugin_context, l3p_id)
        pool = netaddr.IPNetwork(l3p['ip_pool'])
        for cidr in pool.subnet(l3p['subnet_prefix_length']):
            try:
                attrs = {'tenant_id': context.current['tenant_id'],
                         'name': 'epg_' + context.current['name'],
                         'network_id': l2p['network_id'],
                         'ip_version': l3p['ip_version'],
                         'cidr': cidr.__str__(),
                         'enable_dhcp': True,
                         'gateway_ip': attributes.ATTR_NOT_SPECIFIED,
                         'allocation_pools': attributes.ATTR_NOT_SPECIFIED,
                         'dns_nameservers': attributes.ATTR_NOT_SPECIFIED,
                         'host_routes': attributes.ATTR_NOT_SPECIFIED}
                subnet = self._create_subnet(context._plugin_context, attrs)
                subnet_id = subnet['id']
                try:
                    if l3p['routers']:
                        router_id = l3p['routers'][0]
                        interface_info = {'subnet_id': subnet_id}
                        self._add_router_interface(context._plugin_context,
                                                   router_id, interface_info)
                    self._mark_subnet_owned(
                        context._plugin_context.session, subnet_id)
                    context.add_subnet(subnet_id)
                    return
                except n_exc.InvalidInput:
                    # This exception is not expected. We catch this
                    # here so that it isn't caught below and handled
                    # as if the CIDR is already in use.
                    LOG.exception(_("adding subnet to router failed"))
                    self._delete_subnet(context._plugin_context, subnet['id'])
                    raise exc.GroupPolicyInternalError()
            except n_exc.BadRequest:
                # This is expected (CIDR overlap) until we have a
                # proper subnet allocation algorithm. We ignore the
                # exception and repeat with the next CIDR.
                pass
        raise exc.NoSubnetAvailable()

    def _use_explicit_subnet(self, plugin_context, subnet_id, router_id):
        interface_info = {'subnet_id': subnet_id}
        if router_id:
            self._add_router_interface(plugin_context, router_id,
                                       interface_info)

    def _cleanup_subnet(self, plugin_context, subnet_id, router_id):
        interface_info = {'subnet_id': subnet_id}
        if router_id:
            self._remove_router_interface(plugin_context, router_id,
                                          interface_info)
        if self._subnet_is_owned(plugin_context.session, subnet_id):
            self._delete_subnet(plugin_context, subnet_id)

    def _use_implicit_network(self, context):
        attrs = {'tenant_id': context.current['tenant_id'],
                 'name': 'l2p_' + context.current['name'],
                 'admin_state_up': True,
                 'shared': False}
        network = self._create_network(context._plugin_context, attrs)
        network_id = network['id']
        self._mark_network_owned(context._plugin_context.session, network_id)
        context.set_network_id(network_id)

    def _cleanup_network(self, plugin_context, network_id):
        if self._network_is_owned(plugin_context.session, network_id):
            self._delete_network(plugin_context, network_id)

    def _use_implicit_router(self, context):
        attrs = {'tenant_id': context.current['tenant_id'],
                 'name': 'l3p_' + context.current['name'],
                 'external_gateway_info': None,
                 'admin_state_up': True}
        router = self._create_router(context._plugin_context, attrs)
        router_id = router['id']
        self._mark_router_owned(context._plugin_context.session, router_id)
        context.add_router(router_id)

    def _cleanup_router(self, plugin_context, router_id):
        if self._router_is_owned(plugin_context.session, router_id):
            self._delete_router(plugin_context, router_id)

    def _create_contract_sg(self, context, sg_name_prefix):
        # This method sets up the attributes of security group
        attrs = {'tenant_id': context.current['tenant_id'],
                 'name': sg_name_prefix + '_' + context.current['name'],
                 'description': '',
                 'security_group_rules': ''}
        return self._create_sg(context._plugin_context, attrs)

    def _handle_contracts(self, context):
        # This method handles contract => SG mapping
        # context is EPG context

        # for all consumed contracts, simply associate
        # each EP's port from the EPG
        # rules are expected to be filled out already
        consumed_contracts = context.current['consumed_contracts']
        provided_contracts = context.current['provided_contracts']
        subnets = context.current['subnets']
        epg_id = context.current['id']
        if provided_contracts or consumed_contracts:
            contracts = consumed_contracts + provided_contracts
            self._handle_redirect_action(context, contracts)
        self._assoc_sg_to_epg(context, subnets, provided_contracts,
                              consumed_contracts)
        self._update_sgs_on_epg(context, epg_id, provided_contracts,
                                consumed_contracts, "ASSOCIATE")

    def _get_epgs_providing_contract(self, session, contract_id):
        with session.begin(subtransactions=True):
            return (session.query(
                gpdb.EndpointGroupContractProvidingAssociation).
                    filter_by(contract_id=contract_id).first())

    def _get_epgs_consuming_contract(self, session, contract_id):
        try:
            with session.begin(subtransactions=True):
                return (session.query(
                    gpdb.EndpointGroupContractConsumingAssociation).
                        filter_by(contract_id=contract_id).all())
        except sql_exc.NoResultFound:
            return None

    def _set_policy_ipaddress_mapping(self, session, service_policy_id,
                                      endpoint_group, ipaddress):
        with session.begin(subtransactions=True):
            mapping = ServicePolicyEPGIpAddressMapping(
                            service_policy_id=service_policy_id,
                            endpoint_group=endpoint_group,
                            ipaddress=ipaddress)
            session.add(mapping)

    def _get_epg_policy_ipaddress_mapping(self, session, endpoint_group):
        with session.begin(subtransactions=True):
            return (session.query(ServicePolicyEPGIpAddressMapping).
                    filter_by(endpoint_group=endpoint_group).first())

    def _delete_policy_ipaddress_mapping(self, session, endpoint_group):
        with session.begin(subtransactions=True):
            mappings = session.query(
                        ServicePolicyEPGIpAddressMapping).filter_by(
                            endpoint_group=endpoint_group).first()
            for ip_map in mappings:
                session.delete(ip_map)

    def _handle_redirect_action(self, context, contracts):
        for contract_id in contracts:
            epgs_consuming_contract = self._get_epgs_consuming_contract(
                                            context._plugin_context._session,
                                            contract_id)
            epg_providing_contract = self._get_epgs_providing_contract(
                                            context._plugin_context._session,
                                            contract_id)

            #Create the ServiceChain Instance when we have both Provider and
            #consumer EPGs. If Labels are available, they have to be applied
            #here. For now we support a single provider
            if not epgs_consuming_contract or not epg_providing_contract:
                continue

            contract = context._plugin.get_contract(
                context._plugin_context, contract_id)
            for rule_id in contract.get('policy_rules'):
                rule_chain_map = self._get_rule_servicechain_mapping(
                                    context._plugin_context.session, rule_id)
                if rule_chain_map:
                    break  # Only one redirect action per rule
                policy_rule = context._plugin.get_policy_rule(
                                   context._plugin_context, rule_id)
                classifier_id = policy_rule.get("policy_classifier_id")
                for action_id in policy_rule.get("policy_actions"):
                    policy_action = context._plugin.get_policy_action(
                                    context._plugin_context, action_id)
                    if policy_action['action_type'].upper() == "REDIRECT":
                        for epg_consuming_contract in epgs_consuming_contract:
                            sc_instance = self._create_servicechain_instance(
                                            context,
                                            policy_action.get("action_value"),
                                            epg_providing_contract.
                                            endpoint_group_id,
                                            epg_consuming_contract.
                                            endpoint_group_id,
                                            classifier_id)
                            chain_instance_id = sc_instance['id']
                            self._set_rule_servicechain_instance_mapping(
                                        context._plugin_context.session,
                                        rule_id, chain_instance_id)
                            break

    def _cleanup_redirect_action(self, context):
        consumed_contracts = context.current['consumed_contracts']
        provided_contracts = context.current['provided_contracts']
        if not provided_contracts and not consumed_contracts:
            return
        contracts = provided_contracts + consumed_contracts
        for contract_id in contracts:
            epgs_consuming_contract = self._get_epgs_consuming_contract(
                                            context._plugin_context._session,
                                            contract_id)
            epg_providing_contract = self._get_epgs_providing_contract(
                                            context._plugin_context._session,
                                            contract_id)
            #Delete the ServiceChain Instance when we do not have either
            #the Provider or the consumer EPGs
            if not epgs_consuming_contract or not epg_providing_contract:
                contract = context._plugin.get_contract(
                                context._plugin_context, contract_id)
                for rule_id in contract.get('policy_rules'):
                    chain_id_map = self._get_rule_servicechain_mapping(
                                context._plugin_context.session,
                                rule_id)
                    if chain_id_map:
                        self._delete_servicechain_instance(
                            context, chain_id_map.servicechain_instance_id)
                        break  # Only one redirect action per rule

    # The following methods perform the necessary subset of
    # functionality from neutron.api.v2.base.Controller.
    #
    # REVISIT(rkukura): Can we just use the WSGI Controller?  Using
    # neutronclient is also a possibility, but presents significant
    # issues to unit testing as well as overhead and failure modes.

    def _create_port(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context, 'port',
                                     attrs)

    def _update_port(self, plugin_context, port_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context, 'port',
                                     port_id, attrs)

    def _delete_port(self, plugin_context, port_id):
        self._delete_resource(self._core_plugin,
                              plugin_context, 'port', port_id)

    def _create_subnet(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'subnet', attrs)

    def _update_subnet(self, plugin_context, subnet_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'subnet', subnet_id, attrs)

    def _delete_subnet(self, plugin_context, subnet_id):
        self._delete_resource(self._core_plugin, plugin_context, 'subnet',
                              subnet_id)

    def _create_network(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'network', attrs)

    def _delete_network(self, plugin_context, network_id):
        self._delete_resource(self._core_plugin, plugin_context,
                              'network', network_id)

    def _create_router(self, plugin_context, attrs):
        return self._create_resource(self._l3_plugin, plugin_context, 'router',
                                     attrs)

    def _add_router_interface(self, plugin_context, router_id, interface_info):
        self._l3_plugin.add_router_interface(plugin_context,
                                             router_id, interface_info)

    def _remove_router_interface(self, plugin_context, router_id,
                                 interface_info):
        self._l3_plugin.remove_router_interface(plugin_context, router_id,
                                                interface_info)

    def _delete_router(self, plugin_context, router_id):
        self._delete_resource(self._l3_plugin, plugin_context, 'router',
                              router_id)

    def _create_sg(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'security_group', attrs)

    def _update_sg(self, plugin_context, sg_id, attrs):
        return self._update_resouce(self._core_plugin, plugin_context,
                                    'security_group', sg_id, attrs)

    def _delete_sg(self, plugin_context, sg_id):
        self._delete_resource(self._core_plugin, plugin_context,
                              'security_group', sg_id)

    def _create_sg_rule(self, plugin_context, attrs):
        try:
            return self._create_resource(self._core_plugin, plugin_context,
                                         'security_group_rule', attrs)
        except ext_sg.SecurityGroupRuleExists as ex:
            LOG.warn(_('Security Group already exists %s'), ex.message)
            return

    def _update_sg_rule(self, plugin_context, sg_rule_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'security_group_rule', sg_rule_id,
                                     attrs)

    def _delete_sg_rule(self, plugin_context, sg_rule_id):
        self._delete_resource(self._core_plugin, plugin_context,
                              'security_group_rule', sg_rule_id)

    def _restore_ip_to_allocation_pool(self, context, subnet_id, ip_address):
        #TODO(Magesh):Pass subnets and loop on subnets. Better to add logic
        #to Merge the pools together after Fragmentation
        subnet = self._core_plugin.get_subnet(context._plugin_context,
                                              subnet_id)
        allocation_pools = subnet['allocation_pools']
        for allocation_pool in allocation_pools:
            pool_end_ip = allocation_pool.get('end')
            if ip_address == str(netaddr.IPAddress(pool_end_ip) + 1):
                new_last_ip = ip_address
                allocation_pool['end'] = new_last_ip
                del subnet['gateway_ip']
                subnet = self._update_subnet(context._plugin_context,
                                             subnet['id'], subnet)
                return
        #TODO(Magesh):Have to test this logic. Add proper unit tests
        subnet['allocation_pools'].append({"start": ip_address,
                                          "end": ip_address})
        del subnet['gateway_ip']
        subnet = self._update_subnet(context._plugin_context,
                                     subnet['id'], subnet)

    def _remove_ip_from_allocation_pool(self, context, subnet_id, ip_address):
        #TODO(Magesh):Pass subnets and loop on subnets
        subnet = self._core_plugin.get_subnet(context._plugin_context,
                                              subnet_id)
        allocation_pools = subnet['allocation_pools']
        for allocation_pool in reversed(allocation_pools):
            if ip_address == allocation_pool.get('end'):
                new_last_ip = str(netaddr.IPAddress(ip_address) - 1)
                allocation_pool['end'] = new_last_ip
                del subnet['gateway_ip']
                self._update_subnet(context._plugin_context,
                                    subnet['id'], subnet)
                break

    def _get_last_free_ip(self, context, subnets):
        #Hope lock_mode update is not needed
        range_qry = context.session.query(
            models_v2.IPAvailabilityRange).join(
                models_v2.IPAllocationPool)
        for subnet_id in subnets:
            ip_range = range_qry.filter_by(subnet_id=subnet_id).first()
            if not ip_range:
                continue
            ip_address = ip_range['last_ip']
            return ip_address

    def _create_servicechain_instance(self, context, servicechain_spec,
                                      provider_epg, consumer_epg,
                                      classifier_id, config_params=None):
        config_param_values = {}

        epg = context._plugin.get_endpoint_group(context._plugin_context,
                                                 provider_epg)
        network_service_policy_id = epg.get("network_service_policy_id")
        if network_service_policy_id:
            nsp = context._plugin.get_network_service_policy(
                            context._plugin_context, network_service_policy_id)
            service_params = nsp.get("network_service_params")
            #Supporting only one value now
            param_type = service_params[0].get("type")
            if param_type == "ip_single":
                key = service_params[0].get("name")
                servicepolicy_epg_ip_map = self._get_service_policy_ipaddress(
                                            context, provider_epg)
                servicepolicy_ip = servicepolicy_epg_ip_map.get("ipaddress")
                config_param_values[key] = servicepolicy_ip

        attrs = {'tenant_id': context.current['tenant_id'],
                 'name': 'gbp_' + context.current['name'],
                 'description': "",
                 'servicechain_spec': servicechain_spec,
                 'provider_epg': provider_epg,
                 'consumer_epg': consumer_epg,
                 'classifier': classifier_id,
                 'config_param_values': jsonutils.dumps(config_param_values)}
        return self._create_resource(self._servicechain_plugin,
                                     context._plugin_context,
                                     'servicechain_instance', attrs)

    def _delete_servicechain_instance(self, context, servicechain_instance_id):
        self._delete_resource(self._servicechain_plugin,
                              context._plugin_context,
                              'servicechain_instance',
                              servicechain_instance_id)

    def _create_resource(self, plugin, context, resource, attrs):
        # REVISIT(rkukura): Do create.start notification?
        # REVISIT(rkukura): Check authorization?
        # REVISIT(rkukura): Do quota?
        action = 'create_' + resource
        obj_creator = getattr(plugin, action)
        obj = obj_creator(context, {resource: attrs})
        self._nova_notifier.send_network_change(action, {}, {resource: obj})
        # REVISIT(rkukura): Do create.end notification?
        if cfg.CONF.dhcp_agent_notification:
            self._dhcp_agent_notifier.notify(context,
                                             {resource: obj},
                                             resource + '.create.end')
        return obj

    def _update_resource(self, plugin, context, resource, resource_id, attrs):
        # REVISIT(rkukura): Do update.start notification?
        # REVISIT(rkukura): Check authorization?
        obj_getter = getattr(plugin, 'get_' + resource)
        orig_obj = obj_getter(context, resource_id)
        action = 'update_' + resource
        obj_updater = getattr(plugin, action)
        obj = obj_updater(context, resource_id, {resource: attrs})
        self._nova_notifier.send_network_change(action, orig_obj,
                                                {resource: obj})
        # REVISIT(rkukura): Do update.end notification?
        if cfg.CONF.dhcp_agent_notification:
            self._dhcp_agent_notifier.notify(context,
                                             {resource: obj},
                                             resource + '.update.end')
        return obj

    def _delete_resource(self, plugin, context, resource, resource_id):
        # REVISIT(rkukura): Do delete.start notification?
        # REVISIT(rkukura): Check authorization?
        obj_getter = getattr(plugin, 'get_' + resource)
        obj = obj_getter(context, resource_id)
        action = 'delete_' + resource
        obj_deleter = getattr(plugin, action)
        obj_deleter(context, resource_id)
        self._nova_notifier.send_network_change(action, {}, {resource: obj})
        # REVISIT(rkukura): Do delete.end notification?
        if cfg.CONF.dhcp_agent_notification:
            self._dhcp_agent_notifier.notify(context,
                                             {resource: obj},
                                             resource + '.delete.end')

    def _get_resource(self, plugin, context, resource, resource_id):
        obj_getter = getattr(plugin, 'get_' + resource)
        obj = obj_getter(context, resource_id)
        return obj

    def _get_resources(self, plugin, context, resource, filters=[]):
        obj_getter = getattr(plugin, 'get_' + resource + 's')
        obj = obj_getter(context, filters)
        return obj

    @property
    def _core_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        return manager.NeutronManager.get_plugin()

    @property
    def _l3_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        plugins = manager.NeutronManager.get_service_plugins()
        l3_plugin = plugins.get(pconst.L3_ROUTER_NAT)
        if not l3_plugin:
            LOG.error(_("No L3 router service plugin found."))
            raise exc.GroupPolicyDeploymentError()
        return l3_plugin

    @property
    def _servicechain_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        plugins = manager.NeutronManager.get_service_plugins()
        servicechain_plugin = plugins.get(pconst.SERVICECHAIN)
        if not servicechain_plugin:
            LOG.error(_("No Servicechain service plugin found."))
            raise exc.GroupPolicyDeploymentError()
        return servicechain_plugin

    @property
    def _dhcp_agent_notifier(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store notifier.
        if not self._cached_agent_notifier:
            agent_notifiers = getattr(self._core_plugin, 'agent_notifiers', {})
            self._cached_agent_notifier = (
                agent_notifiers.get(const.AGENT_TYPE_DHCP) or
                dhcp_rpc_agent_api.DhcpAgentNotifyAPI())
        return self._cached_agent_notifier

    def _mark_port_owned(self, session, port_id):
        with session.begin(subtransactions=True):
            owned = OwnedPort(port_id=port_id)
            session.add(owned)

    def _port_is_owned(self, session, port_id):
        with session.begin(subtransactions=True):
            return (session.query(OwnedPort).
                    filter_by(port_id=port_id).
                    first() is not None)

    def _mark_subnet_owned(self, session, subnet_id):
        with session.begin(subtransactions=True):
            owned = OwnedSubnet(subnet_id=subnet_id)
            session.add(owned)

    def _subnet_is_owned(self, session, subnet_id):
        with session.begin(subtransactions=True):
            return (session.query(OwnedSubnet).
                    filter_by(subnet_id=subnet_id).
                    first() is not None)

    def _mark_network_owned(self, session, network_id):
        with session.begin(subtransactions=True):
            owned = OwnedNetwork(network_id=network_id)
            session.add(owned)

    def _network_is_owned(self, session, network_id):
        with session.begin(subtransactions=True):
            return (session.query(OwnedNetwork).
                    filter_by(network_id=network_id).
                    first() is not None)

    def _mark_router_owned(self, session, router_id):
        with session.begin(subtransactions=True):
            owned = OwnedRouter(router_id=router_id)
            session.add(owned)

    def _router_is_owned(self, session, router_id):
        with session.begin(subtransactions=True):
            return (session.query(OwnedRouter).
                    filter_by(router_id=router_id).
                    first() is not None)

    def _set_contract_sg_mapping(self, session, contract_id,
                                 consumed_sg_id, provided_sg_id):
        with session.begin(subtransactions=True):
            mapping = ContractSGsMapping(contract_id=contract_id,
                                         consumed_sg_id=consumed_sg_id,
                                         provided_sg_id=provided_sg_id)
            session.add(mapping)

    def _get_contract_sg_mapping(self, session, contract_id):
        with session.begin(subtransactions=True):
            return (session.query(ContractSGsMapping).
                    filter_by(contract_id=contract_id).one())

    def _sg_rule(self, context, sg_id, protocol, port_range, cidr,
                 direction, unset=False):
        port_min, port_max = (gpdb.GroupPolicyDbPlugin.
                              _get_min_max_ports_from_range(port_range))
        attrs = {'tenant_id': context.current['tenant_id'],
                 'security_group_id': sg_id,
                 'direction': direction,
                 'ethertype': const.IPv4,
                 'protocol': protocol,
                 'port_range_min': port_min,
                 'port_range_max': port_max,
                 'remote_ip_prefix': cidr,
                 'remote_group_id': None}
        if unset:
            filters = {}
            for key in attrs:
                value = attrs[key]
                if value:
                    filters[key] = [value]
            rule = self._core_plugin.get_security_group_rules(
                context._plugin_context, filters)
            if rule:
                self._delete_sg_rule(context._plugin_context, rule[0]['id'])
        else:
            return self._create_sg_rule(context._plugin_context, attrs)

    def _sg_ingress_rule(self, context, sg_id, protocol, port_range,
                        cidr, unset=False):
        return self._sg_rule(context, sg_id, protocol, port_range,
                             cidr, 'ingress', unset)

    def _sg_egress_rule(self, context, sg_id, protocol, port_range,
                        cidr, unset=False):
        return self._sg_rule(context, sg_id, protocol, port_range,
                             cidr, 'egress', unset)

    def _assoc_sgs_to_ep(self, context, ep_id, sg_list):
        ep = context._plugin.get_endpoint(context._plugin_context, ep_id)
        port_id = ep['port_id']
        port = self._core_plugin.get_port(context._plugin_context, port_id)
        cur_sg_list = port[ext_sg.SECURITYGROUPS]
        new_sg_list = cur_sg_list + sg_list
        port[ext_sg.SECURITYGROUPS] = new_sg_list
        self._update_port(context._plugin_context, port_id, port)

    def _disassoc_sgs_from_ep(self, context, ep_id, sg_list):
        ep = context._plugin.get_endpoint(context._plugin_context, ep_id)
        port_id = ep['port_id']
        self._disassoc_sgs_from_port(context._plugin_context, port_id, sg_list)

    def _disassoc_sgs_from_port(self, plugin_context, port_id, sg_list):
        port = self._core_plugin.get_port(plugin_context, port_id)
        cur_sg_list = port[ext_sg.SECURITYGROUPS]
        new_sg_list = list(set(cur_sg_list) - set(sg_list))
        port[ext_sg.SECURITYGROUPS] = new_sg_list
        self._update_port(plugin_context, port_id, port)

    def _generate_list_of_sg_from_epg(self, context, epg_id):
        epg = context._plugin.get_endpoint_group(context._plugin_context,
                                                 epg_id)
        provided_contracts = epg['provided_contracts']
        consumed_contracts = epg['consumed_contracts']
        return(self._generate_list_sg_from_contract_list(context,
                                                         provided_contracts,
                                                         consumed_contracts))

    def _generate_list_sg_from_contract_list(self, context,
                                             provided_contracts,
                                             consumed_contracts):
        ret_list = []
        for contract_id in provided_contracts:
            contract_sg_mappings = self._get_contract_sg_mapping(
                context._plugin_context.session, contract_id)
            provided_sg_id = contract_sg_mappings['provided_sg_id']
            ret_list.append(provided_sg_id)

        for contract_id in consumed_contracts:
            contract_sg_mappings = self._get_contract_sg_mapping(
                context._plugin_context.session, contract_id)
            consumed_sg_id = contract_sg_mappings['consumed_sg_id']
            ret_list.append(consumed_sg_id)
        return ret_list

    def _assoc_epg_sg_to_ep(self, context, ep_id, epg_id):
        sg_list = self._generate_list_of_sg_from_epg(context, epg_id)
        self._assoc_sgs_to_ep(context, ep_id, sg_list)

    def _update_sgs_on_ep_with_epg(self, context, epg_id, new_ep_list, op):
        sg_list = self._generate_list_of_sg_from_epg(context, epg_id)
        for ep_id in new_ep_list:
            if op == "ASSOCIATE":
                self._assoc_sgs_to_ep(context, ep_id, sg_list)
            else:
                self._disassoc_sgs_from_ep(context, ep_id, sg_list)

    def _update_sgs_on_epg(self, context, epg_id,
                           provided_contracts, consumed_contracts, op):
        sg_list = self._generate_list_sg_from_contract_list(context,
                                                            provided_contracts,
                                                            consumed_contracts)
        epg = context._plugin.get_endpoint_group(context._plugin_context,
                                                 epg_id)
        endpoint_list = epg['endpoints']
        for ep_id in endpoint_list:
            if op == "ASSOCIATE":
                self._assoc_sgs_to_ep(context, ep_id, sg_list)
            else:
                self._disassoc_sgs_from_ep(context, ep_id, sg_list)

    # context should be EPG
    def _assoc_sg_to_epg(self, context, subnets, provided_contracts,
                         consumed_contracts):
        if not provided_contracts and not consumed_contracts:
            return

        cidr_list = []
        for subnet_id in subnets:
            subnet = self._core_plugin.get_subnet(context._plugin_context,
                                                  subnet_id)
            cidr = subnet['cidr']
            cidr_list.append(cidr)

        prov_cons = ['providing_cidrs', 'consuming_cidrs']
        for pos, contracts in enumerate([provided_contracts,
                                         consumed_contracts]):
            for contract_id in contracts:
                contract = context._plugin.get_contract(
                    context._plugin_context, contract_id)
                contract_sg_mappings = self._get_contract_sg_mapping(
                    context._plugin_context.session, contract_id)
                cidr_mapping = {prov_cons[pos]: cidr_list,
                                prov_cons[pos - 1]: []}
                policy_rules = contract['policy_rules']
                for policy_rule_id in policy_rules:
                    policy_rule = context._plugin.get_policy_rule(
                        context._plugin_context, policy_rule_id)
                    self._add_or_remove_contract_rule(context, policy_rule,
                                                      contract_sg_mappings,
                                                      cidr_mapping)

    def _manage_contract_rules(self, context, contract, policy_rules,
                               unset=False):
        contract_sg_mappings = self._get_contract_sg_mapping(
            context._plugin_context.session, contract['id'])
        contract = context._plugin._get_contract(
                context._plugin_context, contract['id'])
        epg_mapping = self._get_contract_epg_mapping(context, contract)
        cidr_mapping = self._get_epg_cidrs_mapping(context, epg_mapping)
        for policy_rule_id in policy_rules:
            policy_rule = context._plugin.get_policy_rule(
                context._plugin_context, policy_rule_id)

            self._add_or_remove_contract_rule(context, policy_rule,
                                              contract_sg_mappings,
                                              cidr_mapping, unset=unset)

    def _add_or_remove_contract_rule(self, context, policy_rule,
                                     contract_sg_mappings, cidr_mapping,
                                     unset=False):
        in_out = [gconst.GP_DIRECTION_IN, gconst.GP_DIRECTION_OUT]
        prov_cons = [contract_sg_mappings['provided_sg_id'],
                     contract_sg_mappings['consumed_sg_id']]
        cidr_prov_cons = [cidr_mapping['providing_cidrs'],
                          cidr_mapping['consuming_cidrs']]

        classifier_id = policy_rule['policy_classifier_id']
        classifier = context._plugin.get_policy_classifier(
            context._plugin_context, classifier_id)
        protocol = classifier['protocol']
        port_range = classifier['port_range']

        for pos, sg in enumerate(prov_cons):
            if classifier['direction'] in [gconst.GP_DIRECTION_BI,
                                           in_out[pos]]:
                for cidr in cidr_prov_cons[pos - 1]:
                    self._sg_ingress_rule(context, sg, protocol, port_range,
                                          cidr, unset=unset)
            if classifier['direction'] in [gconst.GP_DIRECTION_BI,
                                           in_out[pos - 1]]:
                self._sg_egress_rule(context, sg, protocol, port_range,
                                     '0.0.0.0/0', unset=unset)

    def _apply_contract_rules(self, context, contract, policy_rules):
        if contract['parent_id']:
            parent = context._plugin.get_contract(
                context._plugin_context, contract['parent_id'])
            policy_rules = policy_rules & set(parent['policy_rules'])
        # Don't add rules unallowed by the parent
        self._manage_contract_rules(context, contract, policy_rules)

    def _remove_contract_rules(self, context, contract, policy_rules):
        self._manage_contract_rules(context, contract, policy_rules,
                                    unset=True)

    def _recompute_contracts(self, context, children):
        # Rules in child but not in parent shall be removed
        # Child rules will be set after being filtered by the parent
        for child in children:
            child = context._plugin.get_contract(
                context._plugin_context, child)
            child_rules = set(child['policy_rules'])
            if child['parent_id']:
                parent = context._plugin.get_contract(
                    context._plugin_context, child['parent_id'])
                parent_rules = set(parent['policy_rules'])
                self._remove_contract_rules(context, child,
                                            child_rules - parent_rules)
            # Old parent may have filtered some rules, need to add them again
            self._apply_contract_rules(context, child, child_rules)

    def _ensure_default_security_group(self, plugin_context, tenant_id):
        filters = {'name': ['gbp_default'], 'tenant_id': [tenant_id]}
        default_group = self._core_plugin.get_security_groups(
            plugin_context, filters)
        if not default_group:
            attrs = {'name': 'gbp_default', 'tenant_id': tenant_id,
                     'description': 'default'}
            ret = self._create_sg(plugin_context, attrs)
            return ret['id']
        else:
            return default_group[0]['id']

    def _get_contract_epg_mapping(self, context, contract):
        # REVISIT(ivar): This will be removed once navigability issue is
        # solved (bug/1384398)
        return {
            'providing_epgs': self._get_epgs_by_id(
                context,
                [x['endpoint_group_id'] for x in contract.get(
                    'providing_endpoint_groups', [])]),
            'consuming_epgs': self._get_epgs_by_id(
                context,
                [x['endpoint_group_id'] for x in contract.get(
                    'consuming_endpoint_groups', [])])}

    def _get_epgs_by_id(self, context, ids):
        if ids:
            filters = {'id': ids}
            return context._plugin.get_endpoint_groups(context._plugin_context,
                                                       filters)
        else:
            return []

    def _get_epg_cidrs(self, context, epgs):
        cidrs = []
        for epg in epgs:
            cidrs.extend([self._core_plugin.get_subnet(
                context._plugin_context, x)['cidr'] for x in epg['subnets']])
        return cidrs

    def _get_epg_cidrs_mapping(self, context, epg_mapping):
        return {
            'providing_cidrs': self._get_epg_cidrs(
                context, epg_mapping['providing_epgs']),
            'consuming_cidrs': self._get_epg_cidrs(
                context, epg_mapping['consuming_epgs'])}

    def _set_rule_servicechain_instance_mapping(self, session, rule_id,
                                                servicechain_instance_id):
        with session.begin(subtransactions=True):
            mapping = RuleServiceChainInstanceMapping(
                            rule_id=rule_id,
                            servicechain_instance_id=servicechain_instance_id)
            session.add(mapping)

    def _get_rule_servicechain_mapping(self, session, rule_id):
        with session.begin(subtransactions=True):
            return (session.query(RuleServiceChainInstanceMapping).
                    filter_by(rule_id=rule_id).first())