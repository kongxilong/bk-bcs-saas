# -*- coding: utf-8 -*-
#
# Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community Edition) available.
# Copyright (C) 2017-2019 THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
import json
import logging
import time
import uuid
from urllib.parse import urlparse

from django.conf import settings
from rest_framework import views
from rest_framework.renderers import BrowsableAPIRenderer
from django.utils.translation import ugettext_lazy as _

from backend.accounts import bcs_perm
from backend.apps.constants import ProjectKind
from backend.components.bcs.k8s import K8SClient
from backend.components.bcs.mesos import MesosClient
from backend.utils.cache import rd_client
from backend.utils.error_codes import error_codes
from backend.utils.renderers import BKAPIRenderer
from backend.utils.response import BKAPIResponse
from backend.web_console import constants, pod_life_cycle
from backend.web_console.bcs_client import k8s, mesos
from backend.web_console.utils import get_kubectld_version

from . import utils
from .serializers import K8SWebConsoleSLZ, MesosWebConsoleSLZ
from ..session import session_mgr

logger = logging.getLogger(__name__)


class WebConsoleSession(views.APIView):
    # 缓存的key
    renderer_classes = (BKAPIRenderer, BrowsableAPIRenderer)

    def get_k8s_context(self, request, project_id, cluster_id):
        """获取docker监控信息
        """
        client = K8SClient(request.user.token.access_token, project_id, cluster_id, None)
        slz = K8SWebConsoleSLZ(data=request.query_params, context={'client': client})
        slz.is_valid(raise_exception=True)

        try:
            bcs_context = utils.get_k8s_cluster_context(client, project_id, cluster_id)
        except Exception as error:
            logger.exception("get access cluster context failed: %s", error)
            message = _("获取集群{}【{}】WebConsole 信息失败").format(self.cluster_name, cluster_id)
            # 记录操作日志
            utils.activity_log(project_id, self.cluster_name, request.user.username, False, message)
            # 返回前端消息
            raise error_codes.APIError(
                _("{}，请检查 Deployment【kube-system/bcs-agent】是否正常{}").format(
                    message, settings.COMMON_EXCEPTION_MSG
                ))

        # kubectl版本区别
        kubectld_version = get_kubectld_version(client.version)

        container_id = slz.validated_data.get('container_id')
        if container_id:
            bcs_context['mode'] = k8s.ContainerDirectClient.MODE
            bcs_context['user_pod_name'] = slz.validated_data['pod_name']
            bcs_context.update(slz.validated_data)

        else:
            bcs_context = utils.get_k8s_admin_context(client, bcs_context, settings.WEB_CONSOLE_MODE)

            ctx = {'username': self.request.user.username,
                   'settings': settings,
                   'kubectld_version': kubectld_version,
                   'namespace': constants.NAMESPACE,
                   'pod_spec': utils.get_k8s_pod_spec(client),
                   'username_slug': utils.get_username_slug(self.request.user.username),
                   # 缓存ctx， 清理使用
                   'should_cache_ctx': True}
            ctx.update(bcs_context)
            try:
                pod_life_cycle.ensure_namespace(ctx)
                configmap = pod_life_cycle.ensure_configmap(ctx)
                logger.debug('get configmap %s', configmap)
                pod = pod_life_cycle.ensure_pod(ctx)
                logger.debug('get pod %s', pod)
            except pod_life_cycle.PodLifeError as error:
                logger.error("kubetctl apply error: %s", error)
                utils.activity_log(project_id, self.cluster_name, request.user.username, False, '%s' % error)
                raise error_codes.APIError('%s' % error)
            except Exception as error:
                logger.exception("kubetctl apply error: %s", error)
                utils.activity_log(project_id, self.cluster_name, request.user.username, False, "申请pod资源失败")
                raise error_codes.APIError(_("申请pod资源失败，请稍后再试{}").format(settings.COMMON_EXCEPTION_MSG))

            bcs_context['user_pod_name'] = pod.metadata.name

        return bcs_context

    def get_mesos_context(self, request, project_id, cluster_id):
        """获取mesos context
        """
        client = MesosClient(request.user.token.access_token, project_id, cluster_id, None)
        slz = MesosWebConsoleSLZ(data=request.query_params, context={'client': client})
        slz.is_valid(raise_exception=True)

        context = {
            'short_container_id': slz.validated_data['container_id'][:12],
            'project_kind': request.project.kind,
            'server_address': client._bcs_server_host,
            'user_pod_name': slz.validated_data['container_name'],
        }
        context.update(slz.validated_data)

        exec_id = client.get_container_exec_id(context['host_ip'], context['short_container_id'])
        if not exec_id:
            utils.activity_log(
                project_id, self.cluster_name, request.user.username, False, f'连接{context["user_pod_name"]}失败')

            raise error_codes.APIError(_('连接 {} 失败，请检查容器状态是否正常{}').format(
                context["user_pod_name"], settings.COMMON_EXCEPTION_MSG))
        context['exec_id'] = exec_id
        context['mode'] = mesos.ContainerDirectClient.MODE

        client_context = {
            'access_token': request.user.token.access_token,
            'project_id': project_id,
            'cluster_id': cluster_id,
            'env': client._bcs_server_stag
        }
        context['client_context'] = client_context
        return context

    def get(self, request, project_id, cluster_id):
        """获取session信息
        """
        perm = bcs_perm.Cluster(request, project_id, cluster_id)
        try:
            perm.can_use(raise_exception=True)
        except Exception as error:
            utils.activity_log(project_id, cluster_id, request.user.username, False, _("集群不正确或没有集群使用权限"))
            raise error

        # resource_name字段长度限制32位
        self.cluster_name = perm.res['name'][:32]

        # 获取web-console context信息
        if request.project.kind == ProjectKind.MESOS.value:
            # 添加白名单控制
            from .views_bk import ensure_mesos_wlist
            ensure_mesos_wlist(project_id, cluster_id, request.user.username)
            context = self.get_mesos_context(request, project_id, cluster_id)
        else:
            context = self.get_k8s_context(request, project_id, cluster_id)

        context['username'] = request.user.username
        context.setdefault('namespace', constants.NAMESPACE)
        logger.info(context)

        session = session_mgr.create(project_id, cluster_id)
        session_id = session.set(context)

        # 替换http为ws地址
        bcs_api_url = urlparse(settings.DEVOPS_BCS_API_URL)
        if bcs_api_url.scheme == 'https':
            scheme = 'wss'
        else:
            scheme = 'ws'
        bcs_api_url = bcs_api_url._replace(scheme=scheme)
        # 连接ws的来源方式, 有容器直连(direct)和多tab管理(mgr)
        source = request.query_params.get('source', 'direct')

        ws_url = f'{bcs_api_url.geturl()}/web_console/projects/{project_id}/clusters/{cluster_id}/ws/?session_id={session_id}&source={source}'  # noqa

        data = {
            'session_id': session_id,
            'ws_url': ws_url
        }
        utils.activity_log(project_id, self.cluster_name, request.user.username, True)

        return BKAPIResponse(data, message=_("获取session成功"))
