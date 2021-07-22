# Copyright 2021 Vectorized, Inc.
#
# Use of this software is governed by the Business Source License
# included in the file licenses/BSL.md
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0

import http.client
import json
import logging
import uuid
import requests
import time
import random
import threading

from ducktape.mark.resource import cluster
from ducktape.utils.util import wait_until

from rptest.clients.types import TopicSpec
from rptest.clients.kafka_cat import KafkaCat
from rptest.clients.kafka_cli_tools import KafkaCliTools
from rptest.tests.redpanda_test import RedpandaTest


def create_topic_names(count):
    return list(f"pandaproxy-topic-{uuid.uuid4()}" for _ in range(count))


HTTP_GET_HEADERS = {"Accept": "application/vnd.schemaregistry.v1+json"}

HTTP_POST_HEADERS = {
    "Accept": "application/vnd.schemaregistry.v1+json",
    "Content-Type": "application/vnd.schemaregistry.v1+json"
}

schema1_def = '{"type":"record","name":"myrecord","fields":[{"type":"string","name":"f1"}]}'
schema2_def = '{"type":"record","name":"myrecord","fields":[{"type":"string","name":"f1"},{"type":"string","name":"f2","default":"foo"}]}'
schema3_def = '{"type":"record","name":"myrecord","fields":[{"type":"string","name":"f1"},{"type":"string","name":"f2"}]}'


def run_with_timeout(f, timeout=30):
    class BackgroundThread(threading.Thread):
        def __init__(self):
            super(BackgroundThread, self).__init__()
            self.result = None
            self.complete = threading.Event()

        def run(self):
            self.result = f()
            self.complete.set()

    t = BackgroundThread()
    t.start()
    if t.complete.wait(timeout):
        return t.result
    else:
        raise RuntimeError("Timeout!")


class SchemaRegistryTest(RedpandaTest):
    """
    Test schema registry against a redpanda cluster.
    """
    def __init__(self, context):
        super(SchemaRegistryTest, self).__init__(
            context,
            num_brokers=3,
            enable_pp=True,
            enable_sr=True,
            extra_rp_conf={"auto_create_topics_enabled": False},
            num_cores=1)

        http.client.HTTPConnection.debuglevel = 1
        logging.basicConfig()
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.getLogger().level)
        requests_log.propagate = True

    def _request(self, verb, path, hostname=None, **kwargs):
        """

        :param verb: String, as for first arg to requests.request
        :param path: URI path without leading slash
        :param timeout: Optional requests timeout in seconds
        :return:
        """

        if hostname is None:
            # Pick hostname once: we will retry the same place we got an error,
            # to avoid silently skipping hosts that are persistently broken
            nodes = [n for n in self.redpanda.nodes]
            random.shuffle(nodes)
            node = nodes[0]
            hostname = node.account.hostname

        uri = f"http://{hostname}:8081/{path}"

        if 'timeout' not in kwargs:
            kwargs['timeout'] = 60

        # Error codes that may appear during normal API operation, do not
        # indicate an issue with the service
        acceptable_errors = {409, 422, 404}

        def accept_response(resp):
            return 200 <= resp.status_code < 300 or resp.status_code in acceptable_errors

        self.logger.debug(f"{verb} hostname={hostname} {path} {kwargs}")

        # This is not a retry loop: you get *one* retry to handle issues
        # during startup, after that a failure is a failure.
        r = requests.request(verb, uri, **kwargs)
        if not accept_response(r):
            self.logger.info(
                f"Retrying for error {r.status_code} on {verb} {path} ({r.text})"
            )
            time.sleep(10)
            r = requests.request(verb, uri, **kwargs)
            if accept_response(r):
                self.logger.info(
                    f"OK after retry {r.status_code} on {verb} {path} ({r.text})"
                )
            else:
                self.logger.info(
                    f"Error after retry {r.status_code} on {verb} {path} ({r.text})"
                )

        self.logger.info(
            f"{r.status_code} {verb} hostname={hostname} {path} {kwargs}")

        return r

    def _base_uri(self):
        return f"http://{self.redpanda.nodes[0].account.hostname}:8081"

    def _get_topics(self):
        return requests.get(
            f"http://{self.redpanda.nodes[0].account.hostname}:8082/topics")

    def _create_topics(self,
                       names=create_topic_names(1),
                       partitions=1,
                       replicas=1,
                       cleanup_policy=TopicSpec.CLEANUP_DELETE):
        self.logger.debug(f"Creating topics: {names}")
        kafka_tools = KafkaCliTools(self.redpanda)
        for name in names:
            kafka_tools.create_topic(
                TopicSpec(name=name,
                          partition_count=partitions,
                          replication_factor=replicas))
        assert set(names).issubset(self._get_topics().json())
        return names

    def _get_config(self, headers=HTTP_GET_HEADERS):
        return self._request("GET", "config", headers=headers)

    def _set_config(self, data, headers=HTTP_POST_HEADERS):
        return self._request("PUT", f"config", headers=headers, data=data)

    def _get_config_subject(self, subject, headers=HTTP_GET_HEADERS):
        return self._request("GET", f"config/{subject}", headers=headers)

    def _set_config_subject(self, subject, data, headers=HTTP_POST_HEADERS):
        return self._request("PUT",
                             f"config/{subject}",
                             headers=headers,
                             data=data)

    def _get_schemas_types(self, headers=HTTP_GET_HEADERS):
        return self._request("GET", f"schemas/types", headers=headers)

    def _get_schemas_ids_id(self, id, headers=HTTP_GET_HEADERS):
        return self._request("GET", f"schemas/ids/{id}", headers=headers)

    def _get_subjects(self, headers=HTTP_GET_HEADERS):
        return self._request("GET", f"subjects", headers=headers)

    def _post_subjects_subject_versions(self,
                                        subject,
                                        data,
                                        headers=HTTP_POST_HEADERS,
                                        **kwargs):
        return self._request("POST",
                             f"subjects/{subject}/versions",
                             headers=headers,
                             data=data,
                             **kwargs)

    def _get_subjects_subject_versions_version(self,
                                               subject,
                                               version,
                                               headers=HTTP_GET_HEADERS):
        return self._request("GET",
                             f"subjects/{subject}/versions/{version}",
                             headers=headers)

    def _get_subjects_subject_versions(self,
                                       subject,
                                       deleted=False,
                                       headers=HTTP_GET_HEADERS,
                                       **kwargs):
        return self._request(
            "GET",
            f"subjects/{subject}/versions{'?deleted=true' if deleted else ''}",
            headers=headers,
            **kwargs)

    def _delete_subject(self,
                        subject,
                        permanent=False,
                        headers=HTTP_GET_HEADERS,
                        **kwargs):
        return self._request(
            "DELETE",
            f"subjects/{subject}{'?permanent=true' if permanent else ''}",
            headers=headers,
            **kwargs)

    def _delete_subject_version(self,
                                subject,
                                version,
                                permanent=False,
                                headers=HTTP_GET_HEADERS,
                                **kwargs):
        return self._request(
            "DELETE",
            f"subjects/{subject}/versions/{version}{'?permanent=true' if permanent else ''}",
            headers=headers,
            **kwargs)

    def _post_compatibility_subject_version(self,
                                            subject,
                                            version,
                                            data,
                                            headers=HTTP_POST_HEADERS):
        return self._request(
            "POST",
            f"compatibility/subjects/{subject}/versions/{version}",
            headers=headers,
            data=data)

    @cluster(num_nodes=3)
    def test_schemas_types(self):
        """
        Verify the schema registry returns the supported types
        """
        self.logger.debug(f"Request schema types with no accept header")
        result_raw = self._get_schemas_types(headers={})
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug(f"Request schema types with defautl accept header")
        result_raw = self._get_schemas_types()
        assert result_raw.status_code == requests.codes.ok
        result = result_raw.json()
        assert result == ["AVRO"]

    @cluster(num_nodes=3)
    def test_post_subjects_subject_versions(self):
        """
        Verify posting a schema
        """

        topic = create_topic_names(1)[0]

        self.logger.debug(f"Register a schema against a subject")
        schema_1_data = json.dumps({"schema": schema1_def})

        self.logger.debug("Get empty subjects")
        result_raw = self._get_subjects()
        if result_raw.json() != []:
            self.logger.error(result_raw.json)
        assert result_raw.json() == []

        self.logger.debug("Posting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["id"] == 1

        self.logger.debug("Reposting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["id"] == 1

        self.logger.debug("Reposting schema 1 as a subject value")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-value", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["id"] == 1

        self.logger.debug("Get subjects")
        result_raw = self._get_subjects()
        assert set(result_raw.json()) == {f"{topic}-key", f"{topic}-value"}

        self.logger.debug("Get schema versions for invalid subject")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-invalid")
        assert result_raw.status_code == requests.codes.not_found
        result = result_raw.json()
        assert result["error_code"] == 40401
        assert result["message"] == f"Subject '{topic}-invalid' not found."

        self.logger.debug("Get schema versions for subject key")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-key")
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json() == [1]

        self.logger.debug("Get schema versions for subject value")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-value")
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json() == [1]

        self.logger.debug("Get schema version 1 for invalid subject")
        result_raw = self._get_subjects_subject_versions_version(
            subject=f"{topic}-invalid", version=1)
        assert result_raw.status_code == requests.codes.not_found
        result = result_raw.json()
        assert result["error_code"] == 40401
        assert result["message"] == f"Subject '{topic}-invalid' not found."

        self.logger.debug("Get invalid schema version for subject")
        result_raw = self._get_subjects_subject_versions_version(
            subject=f"{topic}-key", version=2)
        assert result_raw.status_code == requests.codes.not_found
        result = result_raw.json()
        assert result["error_code"] == 40401
        assert result[
            "message"] == f"Subject '{topic}-key' Version 2 not found."

        self.logger.debug("Get schema version 1 for subject key")
        result_raw = self._get_subjects_subject_versions_version(
            subject=f"{topic}-key", version=1)
        assert result_raw.status_code == requests.codes.ok
        result = result_raw.json()
        assert result["name"] == f"{topic}-key"
        assert result["version"] == 1
        # assert result["schema"] == json.dumps(schema_def)

        self.logger.debug("Get latest schema version for subject key")
        result_raw = self._get_subjects_subject_versions_version(
            subject=f"{topic}-key", version="latest")
        assert result_raw.status_code == requests.codes.ok
        result = result_raw.json()
        assert result["name"] == f"{topic}-key"
        assert result["version"] == 1
        # assert result["schema"] == json.dumps(schema_def)

        self.logger.debug("Get invalid schema version")
        result_raw = self._get_schemas_ids_id(id=2)
        assert result_raw.status_code == requests.codes.not_found
        result = result_raw.json()
        assert result["error_code"] == 40401
        assert result["message"] == "Schema 2 not found"

        self.logger.debug("Get schema version 1")
        result_raw = self._get_schemas_ids_id(id=1)
        assert result_raw.status_code == requests.codes.ok
        result = result_raw.json()
        # assert result["schema"] == json.dumps(schema_def)

    @cluster(num_nodes=3)
    def test_config(self):
        """
        Smoketest config endpoints
        """
        self.logger.debug("Get initial global config")
        result_raw = self._get_config()
        assert result_raw.json()["compatibilityLevel"] == "NONE"

        self.logger.debug("Set global config")
        result_raw = self._set_config(
            data=json.dumps({"compatibility": "FULL"}))
        assert result_raw.json()["compatibility"] == "FULL"

        # Check out set write shows up in a read
        result_raw = self._get_config()
        self.logger.debug(
            f"response {result_raw.status_code} {result_raw.text}")
        assert result_raw.json()["compatibilityLevel"] == "FULL"

        self.logger.debug("Get invalid subject config")
        result_raw = self._get_config_subject(subject="invalid_subject")
        assert result_raw.status_code == requests.codes.not_found
        assert result_raw.json()["error_code"] == 40401

        schema_1_data = json.dumps({"schema": schema1_def})

        topic = create_topic_names(1)[0]

        self.logger.debug("Posting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)

        self.logger.debug("Get subject config - should be same as global")
        result_raw = self._get_config_subject(subject=f"{topic}-key")
        assert result_raw.json()["compatibilityLevel"] == "FULL"

        self.logger.debug("Set subject config")
        result_raw = self._set_config_subject(
            subject=f"{topic}-key",
            data=json.dumps({"compatibility": "BACKWARD_TRANSITIVE"}))
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["compatibility"] == "BACKWARD_TRANSITIVE"

        self.logger.debug("Get subject config - should be overriden")
        result_raw = self._get_config_subject(subject=f"{topic}-key")
        assert result_raw.json()["compatibilityLevel"] == "BACKWARD_TRANSITIVE"

    @cluster(num_nodes=3)
    def test_post_compatibility_subject_version(self):
        """
        Verify compatibility
        """

        topic = create_topic_names(1)[0]

        self.logger.debug(f"Register a schema against a subject")
        schema_1_data = json.dumps({"schema": schema1_def})
        schema_2_data = json.dumps({"schema": schema2_def})
        schema_3_data = json.dumps({"schema": schema3_def})

        self.logger.debug("Posting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Set subject config - NONE")
        result_raw = self._set_config_subject(subject=f"{topic}-key",
                                              data=json.dumps(
                                                  {"compatibility": "NONE"}))
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Check compatibility none, no default")
        result_raw = self._post_compatibility_subject_version(
            subject=f"{topic}-key", version=1, data=schema_2_data)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["is_compatible"] == True

        self.logger.debug("Set subject config - BACKWARD")
        result_raw = self._set_config_subject(
            subject=f"{topic}-key",
            data=json.dumps({"compatibility": "BACKWARD"}))
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Check compatibility backward, with default")
        result_raw = self._post_compatibility_subject_version(
            subject=f"{topic}-key", version=1, data=schema_2_data)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["is_compatible"] == True

        self.logger.debug("Check compatibility backward, no default")
        result_raw = self._post_compatibility_subject_version(
            subject=f"{topic}-key", version=1, data=schema_3_data)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json()["is_compatible"] == False

    @cluster(num_nodes=3)
    def test_delete_subject(self):
        """
        Verify delete subject
        """

        topic = create_topic_names(1)[0]

        self.logger.debug(f"Register a schema against a subject")
        schema_1_data = json.dumps({"schema": schema1_def})
        schema_2_data = json.dumps({"schema": schema2_def})
        schema_3_data = json.dumps({"schema": schema3_def})

        self.logger.debug("Posting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Set subject config - NONE")
        result_raw = self._set_config_subject(subject=f"{topic}-key",
                                              data=json.dumps(
                                                  {"compatibility": "NONE"}))
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Posting schema 2 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_2_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Posting schema 3 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_3_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        # Check that permanent delete is refused before soft delete
        self.logger.debug("Prematurely permanently delete subject")
        result_raw = self._delete_subject(subject=f"{topic}-key",
                                          permanent=True)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.not_found

        self.logger.debug("Soft delete subject")
        result_raw = run_with_timeout(
            lambda: self._delete_subject(subject=f"{topic}-key"), 10)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Get versions")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-key")
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.not_found

        self.logger.debug("Get versions - include deleted")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-key", deleted=True)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json() == [1, 2, 3]

        self.logger.debug("Permanently delete subject")
        result_raw = self._delete_subject(subject=f"{topic}-key",
                                          permanent=True)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

    @cluster(num_nodes=3)
    def test_delete_subject_version(self):
        """
        Verify delete subject version
        """

        topic = create_topic_names(1)[0]

        self.logger.debug(f"Register a schema against a subject")
        schema_1_data = json.dumps({"schema": schema1_def})
        schema_2_data = json.dumps({"schema": schema2_def})
        schema_3_data = json.dumps({"schema": schema3_def})

        self.logger.debug("Posting schema 1 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_1_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Set subject config - NONE")
        result_raw = self._set_config_subject(subject=f"{topic}-key",
                                              data=json.dumps(
                                                  {"compatibility": "NONE"}))
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Posting schema 2 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_2_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Posting schema 3 as a subject key")
        result_raw = self._post_subjects_subject_versions(
            subject=f"{topic}-key", data=schema_3_data)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Permanently delete version 2")
        result_raw = self._delete_subject_version(subject=f"{topic}-key",
                                                  version=2,
                                                  permanent=True)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.not_found

        self.logger.debug("Soft delete version 2")
        result_raw = self._delete_subject_version(
            subject=f"{topic}-key",
            version=2,
        )
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok

        self.logger.debug("Get versions")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-key")
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json() == [1, 3]

        self.logger.debug("Get versions - include deleted")
        result_raw = self._get_subjects_subject_versions(
            subject=f"{topic}-key", deleted=True)
        self.logger.debug(result_raw)
        assert result_raw.status_code == requests.codes.ok
        assert result_raw.json() == [1, 2, 3]

    @cluster(num_nodes=3)
    def test_concurrent_writes(self):
        schema_template = '{"type":"record","name":"record_%s","fields":[{"type":"string","name":"f1"}]}'

        # How many errors before a worker gives up?
        max_errs = 1

        # Warm up the servers (schema_registry doesn't create topic etc before first access)
        for node in self.redpanda.nodes:
            r = self._request("GET",
                              "subjects",
                              hostname=node.account.hostname)
            assert r.status_code == requests.codes.ok

        class WriteWorker(threading.Thread):
            def __init__(self, name, count, test):
                super(WriteWorker, self).__init__()
                self.name = name
                self.count = count
                self.test = test
                self.logger = test.logger
                self.schema_counter = 1
                self.results = {}

                self.errors = []

            def create(self):
                for i in range(0, self.count):
                    subject = f"subject_{self.name}_{i}"
                    version_id = 1
                    schema_def = schema_template % f"{self.name}_{i}"

                    self.logger.debug(
                        f"Worker {self.name} writing subject {subject}")
                    try:
                        resp = self.test._post_subjects_subject_versions(
                            subject=subject,
                            data=json.dumps({"schema": schema_def}),
                            timeout=20)
                    except requests.exceptions.RequestException as e:
                        self._push_err(
                            f"{subject}/{version_id} POST exception: {e}")
                    else:
                        self._check_eq(subject, version_id, "post_ret",
                                       resp.status_code, 200)
                        if resp.status_code == 200:
                            schema_id = resp.json()["id"]
                            self.results[(subject, version_id)] = (schema_def,
                                                                   schema_id)

            def _push_err(self, err):
                self.logger.error(f"push_err[{self.name}]: {err}")
                self.errors.append(err)
                if len(self.errors) > max_errs:
                    # On e.g. timeouts, test takes too long if we wait for every request to time out
                    raise RuntimeError("Too many errors on worker {self.name}")

            def _check_eq(self, subject, version, check_name, a, b):
                if a != b:
                    self._push_err(
                        f"{subject}/{version} failed {check_name} {a}!={b}")

            def verify(self):
                for (subject, version), (schema_def,
                                         schema_id) in self.results.items():
                    try:
                        r = self.test._get_subjects_subject_versions_version(
                            subject, version)
                    except requests.exceptions.RequestException as e:
                        self._push_err(
                            f"{subject}/{version} GET version exception: {e}")
                        continue

                    self._check_eq(subject, version, "subject_retcode",
                                   r.status_code, 200)
                    self._check_eq(subject, version, "name",
                                   r.json()['name'], subject)
                    self._check_eq(subject, version, "version",
                                   r.json()['version'], version)
                    self._check_eq(subject, version, "schema",
                                   r.json()['schema'], schema_def)

                    try:
                        r = self.test._get_schemas_ids_id(id=schema_id)
                    except requests.exceptions.RequestException as e:
                        self._push_err(
                            f"{subject}/{version} GET schema exception: {e}")
                        continue
                    self._check_eq(subject, version, "schema_retcode",
                                   r.status_code, 200)
                    self._check_eq(subject, version, "schema_def",
                                   r.json()['schema'], schema_def)

                schema_ids = self.get_schema_ids()
                if len(set(schema_ids)) != len(schema_ids):
                    self._push_err(f"Schema IDs reused!")

            def run(self):
                try:
                    t1 = time.time()
                    self.create()
                    create_dur = time.time() - t1
                    self.logger.info(
                        f"[{self.name}] Created {self.count} in {create_dur}s ({self.count / create_dur}/s)"
                    )

                    # Drop out early if we already found some errors during write
                    if len(self.errors) == 0:
                        self.verify()
                except Exception as e:
                    self.errors.append(f"Exception!  {e}")
                    raise

            def get_schema_ids(self):
                result = []
                for (subject, version), (schema_def,
                                         schema_id) in self.results.items():
                    result.append(schema_id)

                return result

        # Even this relatively small number of concurrent writers is
        # more stress than the system will encounter in the field: schemas
        # are likely to be written occasionally and not from many clients at once.
        # However, it is enough to push the system to hit its collision/retry path
        # for writes.
        subjects_per_worker = 16
        n_workers = 4
        workers = []
        for n in range(0, n_workers):
            worker = WriteWorker(f"{n}", subjects_per_worker, self)
            workers.append(worker)

        for worker in workers:
            worker.start()

        for worker in workers:
            worker.join()

        all_schema_ids = []
        for worker in workers:
            if worker.errors:
                self.logger.error(f"Worker {worker.name} errors:")
                for e in worker.errors:
                    self.logger.error(f"  Worker {worker.name}: {e}")
            else:
                self.logger.info(f"Worker {worker.name} OK")

            all_schema_ids.extend(worker.get_schema_ids())

        assert sum([len(worker.errors) for worker in workers]) == 0

        # Check no schema IDs were duplicated
        assert len(all_schema_ids) == len(set(all_schema_ids))
