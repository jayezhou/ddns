# -*- coding: utf-8 -*-
"""
IPv6 DDNS 上报（阿里云 DNS AAAA 记录）

只上报 IPv6：从本机网卡读取全局公网 IPv6 地址，与阿里云 DNS 中已存在的
AAAA 记录比对，不一致时更新记录。

与 sample.py 的区别：
- 只处理 IPv6（record_type 固定为 AAAA），不做 IPv4
- 公网 IPv6 只从本机网卡读取，不依赖任何外部 IP 查询服务
- AccessKey 从环境变量读取（ACCESS_KEY_ID / ACCESS_KEY_SECRET），不硬编码
"""
import ipaddress
import re
import subprocess
import sys

from Tea.core import TeaCore
from typing import List

from alibabacloud_alidns20150109.client import Client as DnsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_darabonba_env.client import Client as EnvClient
from alibabacloud_alidns20150109 import models as dns_models
from alibabacloud_tea_console.client import Client as ConsoleClient
from alibabacloud_tea_util.client import Client as UtilClient


class Sample:
    def __init__(self):
        pass

    @staticmethod
    def initialization(
        region_id: str,
    ) -> DnsClient:
        """
        Initialization  初始化公共请求参数
        """
        access_key_id = EnvClient.get_env('ACCESS_KEY_ID')
        access_key_secret = EnvClient.get_env('ACCESS_KEY_SECRET')
        if not access_key_id or not access_key_secret:
            raise RuntimeError(
                '缺少环境变量 ACCESS_KEY_ID / ACCESS_KEY_SECRET，请先设置'
            )
        config = open_api_models.Config()
        config.access_key_id = access_key_id
        config.access_key_secret = access_key_secret
        # 您的可用区ID
        config.region_id = region_id
        return DnsClient(config)

    @staticmethod
    def normalize_ipv6(value: str) -> str | None:
        """
        将 IPv6 地址文本归一化为 RFC5952 压缩格式。
        用于比对，避免本地地址与阿里云存储文本格式不一致时误判"IP 已变化"。
        非法输入返回 None。
        """
        try:
            return ipaddress.IPv6Address(value.strip()).compressed
        except Exception:
            return None

    @staticmethod
    def get_local_public_ipv6() -> str | None:
        """
        从本机网卡读取全局公网 IPv6 地址。

        解析 `ip -6 addr` 输出，仅保留 scope=global 的 inet6 地址，
        再用 ipaddress.is_global 二次校验，排除回环/链路本地/ULA 等非公网地址。
        取到任一合法全局地址即返回；读取失败或没有全局地址则返回 None。
        """
        try:
            output = subprocess.run(
                ['ip', '-6', 'addr'],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except Exception as error:
            ConsoleClient.log(f'执行 ip -6 addr 失败：{error}')
            return None

        # 收集所有候选地址（同一行内即可含 scope），再统一过滤
        candidates = []
        for line in output.splitlines():
            match = re.match(
                r"\s+inet6\s+([0-9a-fA-F:]+)/\d+\s+scope\s+(\S+)", line
            )
            if match:
                candidates.append((match.group(1), match.group(2)))

        for addr, scope in candidates:
            if scope != 'global':
                continue
            normalized = Sample.normalize_ipv6(addr)
            if normalized is None:
                continue
            try:
                if ipaddress.IPv6Address(normalized).is_global:
                    return normalized
            except Exception:
                continue
        return None

    @staticmethod
    def describe_domain_records(
        client: DnsClient,
        domain_name: str,
        rr: str,
    ) -> dns_models.DescribeDomainRecordsResponse:
        """
        获取主域名的 AAAA 解析记录列表
        """
        req = dns_models.DescribeDomainRecordsRequest()
        # 主域名
        req.domain_name = domain_name
        # 主机记录
        req.rrkey_word = rr
        # 解析记录类型：IPv6
        req.type = 'AAAA'
        try:
            resp = client.describe_domain_records(req)
            ConsoleClient.log('-------------------获取主域名的 AAAA 解析记录列表--------------------')
            ConsoleClient.log(UtilClient.to_jsonstring(TeaCore.to_map(resp)))
            return resp
        except Exception as error:
            ConsoleClient.log(error.message)
        return

    @staticmethod
    def update_domain_record(
        client: DnsClient,
        req: dns_models.UpdateDomainRecordRequest,
    ) -> None:
        """
        修改解析记录
        """
        try:
            resp = client.update_domain_record(req)
            ConsoleClient.log('-------------------修改解析记录--------------------')
            ConsoleClient.log(UtilClient.to_jsonstring(TeaCore.to_map(resp)))
        except Exception as error:
            ConsoleClient.log(error.message)

    @staticmethod
    def main(
        args: List[str],
    ) -> None:
        regionid = args[0]
        current_host_ipv6 = args[1]
        domain_name = args[2]
        rr = args[3]
        client = Sample.initialization(regionid)
        resp = Sample.describe_domain_records(client, domain_name, rr)
        if UtilClient.is_unset(resp) or UtilClient.is_unset(resp.body.domain_records.record[0]):
            ConsoleClient.log('错误参数！')
            return
        record = resp.body.domain_records.record[0]
        # 记录ID
        record_id = record.record_id
        # 记录值
        records_value = record.value
        # 归一化当前 IPv6，供比较与更新使用
        current_ipv6_norm = Sample.normalize_ipv6(current_host_ipv6)
        if current_ipv6_norm is None:
            ConsoleClient.log('当前公网IPv6格式非法，退出程序')
            return
        ConsoleClient.log(f'-------------------当前主机公网IPv6为：{current_ipv6_norm}--------------------')
        # 双侧归一化后比较，避免文本格式差异导致误判
        if not UtilClient.equal_string(
            current_ipv6_norm,
            Sample.normalize_ipv6(records_value),
        ):
            # 修改解析记录
            req = dns_models.UpdateDomainRecordRequest()
            # 主机记录
            req.rr = rr
            # 记录ID
            req.record_id = record_id
            # 将主机记录值改为当前主机IPv6
            req.value = current_ipv6_norm
            # 解析记录类型
            req.type = 'AAAA'
            Sample.update_domain_record(client, req)
        else:
            ConsoleClient.log('-------------------IPv6未变化，跳过更新--------------------')


if __name__ == '__main__':
    public_ipv6 = Sample.get_local_public_ipv6()
    if not public_ipv6:
        ConsoleClient.log('未获取到公网IPv6地址，退出程序')
        sys.exit(1)
    args = [
        'cn-hangzhou',    # regionid
        public_ipv6,      # current_host_ipv6
        'tpzwl.com',      # domain_name
        'house',          # rr
    ]
    Sample.main(args)
