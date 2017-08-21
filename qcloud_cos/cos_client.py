# -*- coding=utf-8

import requests
import urllib
import logging
import sys
import copy
import xml.dom.minidom
import xml.etree.ElementTree
from requests import Request, Session
from streambody import StreamBody
from xml2dict import Xml2Dict
from cos_auth import CosS3Auth
from cos_exception import CosClientError
from cos_exception import CosServiceError

logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                datefmt='%a, %d %b %Y %H:%M:%S',
                filename='cos_s3.log',
                filemode='w')
logger = logging.getLogger(__name__)
reload(sys)
sys.setdefaultencoding('utf-8')
maplist = {
           'ContentLength': 'Content-Length',
           'ContentType': 'Content-Type',
           'ContentMD5': 'Content-MD5',
           'CacheControl': 'Cache-Control',
           'ContentDisposition': 'Content-Disposition',
           'ContentEncoding': 'Content-Encoding',
           'Expires': 'Expires',
           'Metadata': 'Metadata',
           'ACL': 'x-cos-acl',
           'GrantFullControl': 'x-cos-grant-full-control',
           'GrantWrite': 'x-cos-grant-write',
           'GrantRead': 'x-cos-grant-read',
           'StorageClass': 'x-cos-storage-class',
           'EncodingType': 'encoding-type'
           }


def to_unicode(s):
    if isinstance(s, unicode):
        return s
    else:
        return s.decode('utf-8')


def dict_to_xml(data):
    """V5使用xml格式，将输入的dict转换为xml"""
    doc = xml.dom.minidom.Document()
    root = doc.createElement('CompleteMultipartUpload')
    doc.appendChild(root)

    if 'Part' not in data.keys():
        raise CosClientError("Invalid Parameter, Part Is Required!")

    for i in data['Part']:
        nodePart = doc.createElement('Part')

        if 'PartNumber' not in i.keys():
            raise CosClientError("Invalid Parameter, PartNumber Is Required!")

        nodeNumber = doc.createElement('PartNumber')
        nodeNumber.appendChild(doc.createTextNode(str(i['PartNumber'])))

        if 'ETag' not in i.keys():
            raise CosClientError("Invalid Parameter, ETag Is Required!")

        nodeETag = doc.createElement('ETag')
        nodeETag.appendChild(doc.createTextNode(str(i['ETag'])))

        nodePart.appendChild(nodeNumber)
        nodePart.appendChild(nodeETag)
        root.appendChild(nodePart)
    return doc.toxml('utf-8')


def xml_to_dict(data):
    """V5使用xml格式，将response中的xml转换为dict"""
    root = xml.etree.ElementTree.fromstring(data)
    xmldict = Xml2Dict(root)
    return xmldict


def get_id_from_xml(data, name):
    """解析xml中的特定字段"""
    tree = xml.dom.minidom.parseString(data)
    root = tree.documentElement
    result = root.getElementsByTagName(name)
    # use childNodes to get a list, if has no child get itself
    return result[0].childNodes[0].nodeValue


def mapped(headers):
    """S3到COS参数的一个映射"""
    _headers = dict()
    for i in headers.keys():
        if i in maplist:
            _headers[maplist[i]] = headers[i]
        else:
            raise CosClientError('No Parameter Named '+i+' Please Check It')
    return _headers


class CosConfig(object):
    """config类，保存用户相关信息"""
    def __init__(self, Appid, Region, Access_id, Access_key):
        self._appid = Appid
        self._region = Region
        self._access_id = Access_id
        self._access_key = Access_key
        logger.info("config parameter-> appid: {appid}, region: {region}".format(
                 appid=Appid,
                 region=Region))

    def uri(self, bucket, path=None):
        """拼接url"""
        if path:
            if path[0] == '/':
                path = path[1:]
            url = u"http://{bucket}-{uid}.{region}.myqcloud.com/{path}".format(
                bucket=to_unicode(bucket),
                uid=self._appid,
                region=self._region,
                path=to_unicode(path)
            )
        else:
            url = u"http://{bucket}-{uid}.{region}.myqcloud.com".format(
                bucket=to_unicode(bucket),
                uid=self._appid,
                region=self._region
            )
        return url


class CosS3Client(object):
    """cos客户端类，封装相应请求"""
    def __init__(self, conf, retry=2, session=None):
        self._conf = conf
        self._retry = retry  # 重试的次数，分片上传时可适当增大
        if session is None:
            self._session = requests.session()
        else:
            self._session = session

    def get_auth(self, Method, Bucket, Key=None, **kwargs):
        """获取签名"""
        headers = mapped(kwargs)
        # TODO(tiedu)  检查header的参数合法性
        url = self._conf.uri(bucket=Bucket, path=Key)
        r = Request(Method, url)
        auth = CosS3Auth(self._conf._access_id, self._conf._access_key)
        return auth(r).headers['Authorization']

    def send_request(self, method, url, timeout=30, **kwargs):
        try:
            for j in range(self._retry):
                if method == 'POST':
                    res = self._session.post(url, timeout=timeout, **kwargs)
                elif method == 'GET':
                    res = self._session.get(url, timeout=timeout, **kwargs)
                elif method == 'PUT':
                    res = self._session.put(url, timeout=timeout, **kwargs)
                elif method == 'DELETE':
                    res = self._session.delete(url, timeout=timeout, **kwargs)
                elif method == 'HEAD':
                    res = self._session.head(url, timeout=timeout, **kwargs)
                if res.status_code < 300:
                    return res
        except Exception as e:  # 捕获requests抛出的如timeout等客户端错误,转化为客户端错误
            logger.exception('url:%s, exception:%s' % (url, str(e)))
            raise CosClientError(str(e))

        if res.status_code >= 300:  # 所有的3XX,4XX,5XX都认为是COSServiceError
            msg = res.text
            logger.error(msg)
            raise CosServiceError(msg, res.status_code)

    def put_object(self, Bucket, Body, Key, **kwargs):
        """单文件上传接口，适用于小文件，最大不得超过5GB"""
        headers = mapped(kwargs)
        if 'Metadata' in headers.keys():
            for i in headers['Metadata'].keys():
                headers[i] = headers['Metadata'][i]
            headers.pop('Metadata')

        url = self._conf.uri(bucket=Bucket, path=Key)
        logger.info("put object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        for j in range(self._retry):
            rt = self.send_request(
                method='PUT',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                data=Body,
                headers=headers)
            if rt is None:
                continue
            if rt.status_code == 200:
                break
            logger.error(rt.text)

        response = dict()
        response['ETag'] = rt.headers['ETag']
        return response

    def get_object(self, Bucket, Key, **kwargs):
        """单文件下载接口"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key)
        logger.info("get object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='GET',
                url=url,
                stream=True,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)

        response = dict()
        response['Body'] = StreamBody(rt)

        for k in rt.headers.keys():
            response[k] = rt.headers[k]
        return response

    def get_presigned_download_url(self, Bucket, Key):
        """生成预签名的下载url"""
        url = self._conf.uri(bucket=Bucket, path=Key)
        sign = self.get_auth(Method='GET', Bucket=Bucket, Key=Key)
        url = url + '?sign=' + urllib.quote(sign)
        return url

    def delete_object(self, Bucket, Key, **kwargs):
        """单文件删除接口"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key)
        logger.info("delete object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='DELETE',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        return None

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        """创建分片上传，适用于大文件上传"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key+"?uploads")
        logger.info("create multipart upload, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='POST',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)

        data = xml_to_dict(rt.text)
        return data

    def upload_part(self, Bucket, Key, Body, PartNumber, UploadId, **kwargs):
        """上传分片，单个大小不得超过5GB"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key+"?partNumber={PartNumber}&uploadId={UploadId}".format(
            PartNumber=PartNumber,
            UploadId=UploadId))
        logger.info("put object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='PUT',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                data=Body)
        response = dict()
        response['ETag'] = rt.headers['ETag']
        return response

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload={}, **kwargs):
        """完成分片上传，组装后的文件不得小于1MB,否则会返回错误"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key+"?uploadId={UploadId}".format(UploadId=UploadId))
        logger.info("complete multipart upload, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='POST',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                data=dict_to_xml(MultipartUpload),
                timeout=1200,  # 分片上传大文件的时间比较长，设置为20min
                headers=headers)
        response = dict()
        data = xml_to_dict(rt.text)
        for key in data.keys():
            response[key[key.find('}')+1:]] = data[key]
        return response

    def abort_multipart_upload(self, Bucket, Key, UploadId, **kwargs):
        """放弃一个已经存在的分片上传任务，删除所有已经存在的分片"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key+"?uploadId={UploadId}".format(UploadId=UploadId))
        logger.info("abort multipart upload, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='DELETE',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        return None

    def list_parts(self, Bucket, Key, UploadId, **kwargs):
        """列出已上传的分片"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key+"?uploadId={UploadId}".format(UploadId=UploadId))
        logger.info("list multipart upload, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='GET',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)

        data = xml_to_dict(rt.text)
        if 'Part' in data.keys():
            if isinstance(data['Part'], list):
                return data
            else:  # 只有一个part，将dict转为list，保持一致
                lst = []
                lst.append(data['Part'])
                data['Part'] = lst
                return data
        else:
            return data

    def create_bucket(self, Bucket, **kwargs):
        """创建一个bucket"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket)
        logger.info("create bucket, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='PUT',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        return None

    def delete_bucket(self, Bucket, **kwargs):
        """删除一个bucket，bucket必须为空"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket)
        logger.info("delete bucket, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='DELETE',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        return None

    def list_objects(self, Bucket, Delimiter="", Marker="", MaxKeys=1000, Prefix="",  **kwargs):
        """获取文件列表"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket)
        logger.info("list objects, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        params = {
            'delimiter': Delimiter,
            'marker': Marker,
            'max-keys': MaxKeys,
            'prefix': Prefix}
        rt = self.send_request(
                method='GET',
                url=url,
                params=params,
                headers=headers,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key))

        data = xml_to_dict(rt.text)
        if 'Contents' in data.keys():
            if isinstance(data['Contents'], list):
                return data
            else:  # 只有一个Contents，将dict转为list，保持一致
                lst = []
                lst.append(data['Contents'])
                data['Contents'] = lst
                return data
        else:
            return data

    def head_object(self, Bucket, Key, **kwargs):
        """获取文件信息"""
        headers = mapped(kwargs)
        url = self._conf.uri(bucket=Bucket, path=Key)
        logger.info("head object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='HEAD',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        return rt.headers

    def gen_copy_source_url(self, CopySource):
        """拼接拷贝源url"""
        if 'Bucket' in CopySource.keys():
            bucket = CopySource['Bucket']
        else:
            raise CosClientError('CopySource Need Parameter Bucket')
        if 'Key' in CopySource.keys():
            key = CopySource['Key']
        else:
            raise CosClientError('CopySource Need Parameter Key')
        url = self._conf.uri(bucket=bucket, path=key).encode('utf8')
        url = url[7:]  # copysource不支持http://开头，去除
        return url

    def copy_object(self, Bucket, Key, CopySource, CopyStatus='Copy', **kwargs):
        """文件拷贝，文件信息修改"""
        headers = mapped(kwargs)
        headers['x-cos-copy-source'] = self.gen_copy_source_url(CopySource)
        headers['x-cos-metadata-directive'] = CopyStatus
        url = self._conf.uri(bucket=Bucket, path=Key)
        logger.info("copy object, url=:{url} ,headers=:{headers}".format(
            url=url,
            headers=headers))
        rt = self.send_request(
                method='PUT',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                headers=headers)
        data = xml_to_dict(rt.text)
        return data

    def list_buckets(self):
        """列出所有bucket"""
        url = 'http://service.cos.myqcloud.com/'
        rt = self.send_request(
                method='GET',
                url=url,
                auth=CosS3Auth(self._conf._access_id, self._conf._access_key),
                )
        data = xml_to_dict(rt.text)
        return data

if __name__ == "__main__":
    pass
