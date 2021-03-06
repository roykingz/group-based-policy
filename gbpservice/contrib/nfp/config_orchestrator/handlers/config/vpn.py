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

import ast
import copy
from gbpservice.contrib.nfp.config_orchestrator.common import common
from gbpservice.nfp.core import log as nfp_logging
from gbpservice.nfp.lib import transport

from neutron_vpnaas.db.vpn import vpn_db

from oslo_log import helpers as log_helpers
import oslo_messaging as messaging

LOG = nfp_logging.getLogger(__name__)

"""
RPC handler for VPN service
"""


class VpnAgent(vpn_db.VPNPluginDb, vpn_db.VPNPluginRpcDbMixin):
    RPC_API_VERSION = '1.0'
    target = messaging.Target(version=RPC_API_VERSION)

    def __init__(self, conf, sc):
        super(VpnAgent, self).__init__()
        self._conf = conf
        self._sc = sc
        self._db_inst = super(VpnAgent, self)

    def _get_dict_desc_from_string(self, vpn_svc):
        svc_desc = vpn_svc.split(";")
        desc = {}
        for ele in svc_desc:
            s_ele = ele.split("=")
            desc.update({s_ele[0]: s_ele[1]})
        return desc

    def _get_vpn_context(self, context, tenant_id, vpnservice_id,
                         ikepolicy_id, ipsecpolicy_id,
                         ipsec_site_conn_id, desc):
        vpnservices = self._get_vpnservices(context, tenant_id,
                                            vpnservice_id, desc)
        ikepolicies = self._get_ikepolicies(context, tenant_id,
                                            ikepolicy_id)
        ipsecpolicies = self._get_ipsecpolicies(context, tenant_id,
                                                ipsecpolicy_id)
        ipsec_site_conns = self._get_ipsec_site_conns(context, tenant_id,
                                                      ipsec_site_conn_id, desc)

        return {'vpnservices': vpnservices,
                'ikepolicies': ikepolicies,
                'ipsecpolicies': ipsecpolicies,
                'ipsec_site_conns': ipsec_site_conns}

    def _get_core_context(self, context):
        return {'networks': common.get_networks(context, self._conf.host),
                'routers': common.get_routers(context, self._conf.host)}

    def _context(self, context, tenant_id, resource, resource_data):
        if context.is_admin:
            tenant_id = context.tenant_id
        if resource.lower() == 'ipsec_site_connection':
            vpn_ctx_db = self._get_vpn_context(context,
                                               tenant_id,
                                               resource_data[
                                                   'vpnservice_id'],
                                               resource_data[
                                                   'ikepolicy_id'],
                                               resource_data[
                                                   'ipsecpolicy_id'],
                                               resource_data['id'],
                                               resource_data[
                                                   'description'])
            core_db = self._get_core_context(context)
            filtered_core_db = self._filter_core_data(core_db,
                                                      vpn_ctx_db[
                                                          'vpnservices'])
            vpn_ctx_db.update(filtered_core_db)
            return vpn_ctx_db
        elif resource.lower() == 'vpn_service':
            return {'vpnservices': [resource_data]}
        else:
            return None

    def _prepare_resource_context_dicts(self, context, tenant_id,
                                        resource, resource_data):
        # Prepare context_dict
        ctx_dict = context.to_dict()
        # Collecting db entry required by configurator.
        # Addind service_info to neutron context and sending
        # dictionary format to the configurator.
        db = self._context(context, tenant_id, resource,
                           resource_data)
        rsrc_ctx_dict = copy.deepcopy(ctx_dict)
        rsrc_ctx_dict.update({'service_info': db})
        return ctx_dict, rsrc_ctx_dict

    def _data_wrapper(self, context, tenant_id, nf, **kwargs):
        nfp_context = {}
        str_description = nf['description'].split('\n')[1]
        description = self._get_dict_desc_from_string(
            str_description)
        resource = kwargs['rsrc_type']
        resource_data = kwargs['resource']
        resource_data['description'] = str_description
        if resource.lower() == 'ipsec_site_connection':
            nfp_context = {'network_function_id': nf['id'],
                           'ipsec_site_connection_id': kwargs[
                               'rsrc_id']}

        ctx_dict, rsrc_ctx_dict = self.\
            _prepare_resource_context_dicts(context, tenant_id,
                                            resource, resource_data)
        nfp_context.update({'neutron_context': ctx_dict,
                            'requester': 'nas_service',
                            'logging_context':
                                nfp_logging.get_logging_context()})
        resource_type = 'vpn'
        kwargs.update({'neutron_context': rsrc_ctx_dict})
        body = common.prepare_request_data(nfp_context, resource,
                                           resource_type, kwargs,
                                           description['service_vendor'])
        return body

    def _fetch_nf_from_resource_desc(self, desc):
        desc_dict = ast.literal_eval(desc)
        nf_id = desc_dict['network_function_id']
        return nf_id

    @log_helpers.log_method_call
    def vpnservice_updated(self, context, **kwargs):
        # Fetch nf_id from description of the resource
        nf_id = self._fetch_nf_from_resource_desc(kwargs[
            'resource']['description'])
        nfp_logging.store_logging_context(meta_id=nf_id)
        nf = common.get_network_function_details(context, nf_id)
        reason = kwargs['reason']
        body = self._data_wrapper(context, kwargs[
            'resource']['tenant_id'], nf, **kwargs)
        transport.send_request_to_configurator(self._conf,
                                               context, body,
                                               reason)
        nfp_logging.clear_logging_context()

    def _filter_core_data(self, db_data, vpnservices):
        filtered_core_data = {'subnets': [],
                              'routers': []}
        for vpnservice in vpnservices:
            subnet_id = vpnservice['subnet_id']
            for network in db_data['networks']:
                subnets = network['subnets']
                for subnet in subnets:
                    if subnet['id'] == subnet_id:
                        filtered_core_data['subnets'].append(
                            {'id': subnet['id'], 'cidr': subnet['cidr']})
            router_id = vpnservice['router_id']
            for router in db_data['routers']:
                if router['id'] == router_id:
                    filtered_core_data['routers'].append({'id': router_id})
        return filtered_core_data

    def _get_vpnservices(self, context, tenant_id, vpnservice_id, desc):
        filters = {'tenant_id': [tenant_id],
                   'id': [vpnservice_id]}
        args = {'context': context, 'filters': filters}
        vpnservices = self._db_inst.get_vpnservices(**args)
        for vpnservice in vpnservices:
            vpnservice['description'] = desc
        return vpnservices

    def _get_ikepolicies(self, context, tenant_id, ikepolicy_id):
        filters = {'tenant_id': [tenant_id],
                   'id': [ikepolicy_id]}
        args = {'context': context, 'filters': filters}
        return self._db_inst.get_ikepolicies(**args)

    def _get_ipsecpolicies(self, context, tenant_id, ipsecpolicy_id):
        filters = {'tenant_id': [tenant_id],
                   'id': [ipsecpolicy_id]}
        args = {'context': context, 'filters': filters}
        return self._db_inst.get_ipsecpolicies(**args)

    def _get_ipsec_site_conns(self, context, tenant_id, ipsec_site_conn_id,
                              desc):
        filters = {'tenant_id': [tenant_id],
                   'id': [ipsec_site_conn_id]}
        args = {'context': context, 'filters': filters}
        ipsec_site_conns = self._db_inst.get_ipsec_site_connections(**args)
        for ipsec_site_conn in ipsec_site_conns:
            ipsec_site_conn['description'] = desc
        return ipsec_site_conns
