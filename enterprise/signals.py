"""
Django signal handlers.
"""

from logging import getLogger

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from enterprise import models, roles_api
from enterprise.api import activate_admin_permissions
from enterprise.api_client.enterprise_catalog import EnterpriseCatalogApiClient
from enterprise.decorators import disable_for_loaddata
from enterprise.tasks import create_enterprise_enrollment
from enterprise.utils import (
    NotConnectedToOpenEdX,
    get_default_catalog_content_filter,
    unset_enterprise_learner_language,
    unset_language_of_all_enterprise_learners,
)
from integrated_channels.blackboard.models import BlackboardEnterpriseCustomerConfiguration
from integrated_channels.canvas.models import CanvasEnterpriseCustomerConfiguration
from integrated_channels.cornerstone.models import CornerstoneEnterpriseCustomerConfiguration
from integrated_channels.degreed2.models import Degreed2EnterpriseCustomerConfiguration
from integrated_channels.degreed.models import DegreedEnterpriseCustomerConfiguration
from integrated_channels.integrated_channel.tasks import mark_orphaned_content_metadata_audit
from integrated_channels.moodle.models import MoodleEnterpriseCustomerConfiguration
from integrated_channels.sap_success_factors.models import SAPSuccessFactorsEnterpriseCustomerConfiguration

try:
    from common.djangoapps.student.models import CourseEnrollment
    from openedx_events.learning.signals import COURSE_ENROLLMENT_CHANGED, COURSE_UNENROLLMENT_COMPLETED

except ImportError:
    CourseEnrollment = None
    COURSE_ENROLLMENT_CHANGED = None
    COURSE_UNENROLLMENT_COMPLETED = None

logger = getLogger(__name__)
_UNSAVED_FILEFIELD = 'unsaved_filefield'
INTEGRATED_CHANNELS = [
    BlackboardEnterpriseCustomerConfiguration,
    CanvasEnterpriseCustomerConfiguration,
    CornerstoneEnterpriseCustomerConfiguration,
    DegreedEnterpriseCustomerConfiguration,
    Degreed2EnterpriseCustomerConfiguration,
    MoodleEnterpriseCustomerConfiguration,
    SAPSuccessFactorsEnterpriseCustomerConfiguration,
]

# Default number of seconds to use as task countdown
# if not otherwise specified via Django settings.
DEFAULT_COUNTDOWN = 3


@disable_for_loaddata
def handle_user_post_save(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Handle User model changes. Context: This signal runs any time a user logs in, including b2c users.

    Steps:

    1. Check for existing PendingEnterpriseCustomerUser(s) for user's email. If one
       or more exists, create an EnterpriseCustomerUser record for each which will
       ensure the user has the "enterprise_learner" role.

    2. When we get a new EnterpriseCustomerUser record (or an existing record if
       one existed), check if the PendingEnterpriseCustomerUser has any pending
       course enrollments. If so, enroll the user in these courses.

    3. Delete the PendingEnterpriseCustomerUser record as its no longer needed.

    4. Using the newly created EnterpriseCustomerUser (or an existing record if one
       existed), check if there is a PendingEnterpriseCustomerAdminUser. If so,
       create an EnterpriseCustomerAdmin record and ensure the user has the
       "enterprise_admin" role.
    """
    created = kwargs.get("created", False)
    user_instance = kwargs.get("instance", None)

    if user_instance is None:
        return  # should never happen, but better safe than 500 error

    pending_ecus = models.PendingEnterpriseCustomerUser.objects.filter(user_email=user_instance.email)

    # link PendingEnterpriseCustomerUser to the EnterpriseCustomer and fulfill pending enrollments
    for pending_ecu in pending_ecus:
        enterprise_customer_user = pending_ecu.link_pending_enterprise_user(
            user=user_instance,
            is_user_created=created,
        )
        pending_ecu.fulfill_pending_course_enrollments(enterprise_customer_user)
        pending_ecu.fulfill_pending_group_memberships(enterprise_customer_user)
        pending_ecu.delete()

    enterprise_customer_users = models.EnterpriseCustomerUser.objects.filter(user_id=user_instance.id)
    for enterprise_customer_user in enterprise_customer_users:
        # activate admin permissions for an existing EnterpriseCustomerUser(s), if applicable
        activate_admin_permissions(enterprise_customer_user)


@receiver(pre_save, sender=models.EnterpriseCustomer)
def update_lang_pref_of_all_learners(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """
    Update the language preference of all the learners belonging to the enterprise customer.
    Set the language preference to the value enterprise customer has used as the `default_language`.
    """
    # Unset the language preference when a new learner is linked with the enterprise customer.
    # The middleware in the enterprise will handle the cases for setting a proper language for the learner.
    if instance.default_language:
        prev_state = models.EnterpriseCustomer.objects.filter(uuid=instance.uuid).first()
        if prev_state is None or prev_state.default_language != instance.default_language:
            # Unset the language preference of all the learners linked with the enterprise customer.
            # The middleware in the enterprise will handle the cases for setting a proper language for the learner.
            unset_language_of_all_enterprise_learners(instance)


@receiver(pre_save, sender=models.EnterpriseCustomerBrandingConfiguration)
def skip_saving_logo_file(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    To avoid saving the logo image at an incorrect path, skip saving it.
    """
    if not instance.id and not hasattr(instance, _UNSAVED_FILEFIELD):
        setattr(instance, _UNSAVED_FILEFIELD, instance.logo)
        instance.logo = None


@receiver(post_save, sender=models.EnterpriseCustomerBrandingConfiguration)
def save_logo_file(sender, instance, **kwargs):            # pylint: disable=unused-argument
    """
    Now that the object is instantiated and instance.id exists, save the image at correct path and re-save the model.
    """
    if kwargs['created'] and hasattr(instance, _UNSAVED_FILEFIELD):
        instance.logo = getattr(instance, _UNSAVED_FILEFIELD)
        delattr(instance, _UNSAVED_FILEFIELD)
        instance.save()


@receiver(post_save, sender=models.EnterpriseCustomerCatalog, dispatch_uid='default_content_filter')
def default_content_filter(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Set default value for `EnterpriseCustomerCatalog.content_filter` if not already set.
    """
    if kwargs['created'] and not instance.content_filter:
        instance.content_filter = get_default_catalog_content_filter()
        instance.save()


@receiver(post_delete, sender=models.EnterpriseCustomerUser)
def delete_enterprise_admin_role_assignment(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Delete the associated enterprise admin role assignment record when deleting an EnterpriseCustomerUser record.
    """
    if instance.user:
        roles_api.delete_admin_role_assignment(
            user=instance.user,
            enterprise_customer=instance.enterprise_customer,
        )


@receiver(post_save, sender=models.EnterpriseCustomerUser)
def assign_or_delete_enterprise_learner_role(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Assign or delete enterprise_learner role for EnterpriseCustomerUser when created or updated.

    The enterprise_learner role is assigned when a new EnterpriseCustomerUser record is
    initially created and removed when a EnterpriseCustomerUser record is updated and
    unlinked (i.e., soft delete - see ENT-2538).
    """
    if not instance.user:
        return

    if kwargs['created']:
        roles_api.assign_learner_role(
            instance.user,
            enterprise_customer=instance.enterprise_customer,
        )
    elif not kwargs['created']:
        # EnterpriseCustomerUser record was updated
        if instance.linked:
            roles_api.assign_learner_role(
                instance.user,
                enterprise_customer=instance.enterprise_customer,
            )
        else:
            roles_api.delete_learner_role_assignment(
                user=instance.user,
                enterprise_customer=instance.enterprise_customer,
            )


@receiver(post_delete, sender=models.EnterpriseCustomerUser)
def delete_enterprise_learner_role_assignment(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Delete the associated enterprise learner role assignment record when deleting an EnterpriseCustomerUser record.
    """
    if not instance.user:
        return

    roles_api.delete_learner_role_assignment(
        user=instance.user,
        enterprise_customer=instance.enterprise_customer,
    )


@receiver(post_save, sender=models.EnterpriseCustomerUser)
def update_learner_language_preference(sender, instance, created, **kwargs):     # pylint: disable=unused-argument
    """
    Update the language preference of the learner.
    Set the language preference to the value enterprise customer has used as the `default_language`.
    """
    # Unset the language preference when a new learner is linked with the enterprise customer.
    # The middleware in the enterprise will handle the cases for setting a proper language for the learner.
    if created and instance.enterprise_customer.default_language:
        unset_enterprise_learner_language(instance)


@receiver(post_save, sender=models.PendingEnterpriseCustomerAdminUser)
def create_pending_enterprise_admin_user(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """
    Creates a PendingEnterpriseCustomerUser when a PendingEnterpriseCustomerAdminUser is created.
    """
    models.PendingEnterpriseCustomerUser.objects.get_or_create(
        enterprise_customer=instance.enterprise_customer,
        user_email=instance.user_email,
    )


@receiver(post_delete, sender=models.PendingEnterpriseCustomerAdminUser)
def delete_pending_enterprise_admin_user(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """
    Deletes a PendingEnterpriseCustomerUser when its associated PendingEnterpriseCustomerAdminUser is removed.
    """
    models.PendingEnterpriseCustomerUser.objects.filter(
        enterprise_customer=instance.enterprise_customer,
        user_email=instance.user_email,
    ).delete()


@receiver(post_save, sender=models.EnterpriseCatalogQuery)
def update_enterprise_catalog_query(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Sync data changes from Enterprise Catalog Query to the Enterprise Customer Catalog.
    """
    updated_content_filter = instance.content_filter
    logger.info(
        'Running update_enterprise_catalog_query for Catalog Query {} with updated_content_filter {}'.format(
            instance.pk,
            updated_content_filter
        )
    )
    catalogs = instance.enterprise_customer_catalogs.all()

    for catalog in catalogs:
        logger.info(
            'update_enterprise_catalog_query is updating catalog {} with the updated_content_filter.'.format(
                catalog.uuid
            )
        )
        catalog.content_filter = updated_content_filter
        catalog.save()  # This save will trigger the update_enterprise_catalog_data() receiver below


@receiver(post_save, sender=models.EnterpriseCustomerCatalog)
def update_enterprise_catalog_data(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Send data changes to Enterprise Catalogs to the Enterprise Catalog Service.

    Additionally sends a request to update the catalog's metadata from discovery, and index any relevant content for
    Algolia.
    """
    catalog_uuid = instance.uuid
    catalog_query_uuid = str(instance.enterprise_catalog_query.uuid) if instance.enterprise_catalog_query else None
    query_title = getattr(instance.enterprise_catalog_query, 'title', None)
    include_exec_ed_2u_courses = getattr(instance.enterprise_catalog_query, 'include_exec_ed_2u_courses', False)
    try:
        catalog_client = EnterpriseCatalogApiClient()
        if kwargs['created']:
            response = catalog_client.get_enterprise_catalog(
                catalog_uuid=catalog_uuid,
                # Suppress 404 exception on create since we do not expect the catalog
                # to exist yet in enterprise-catalog
                should_raise_exception=False,
            )
        else:
            response = catalog_client.get_enterprise_catalog(catalog_uuid=catalog_uuid)
    except NotConnectedToOpenEdX as exc:
        logger.exception(
            'Unable to update Enterprise Catalog {}'.format(str(catalog_uuid)), exc_info=exc
        )
    else:
        if not response:
            # catalog with matching uuid does NOT exist in enterprise-catalog
            # service, so we should create a new catalog
            catalog_client.create_enterprise_catalog(
                str(catalog_uuid),
                str(instance.enterprise_customer.uuid),
                instance.enterprise_customer.name,
                instance.title,
                instance.content_filter,
                instance.enabled_course_modes,
                instance.publish_audit_enrollment_urls,
                catalog_query_uuid,
                query_title,
                include_exec_ed_2u_courses,
            )
        else:
            # catalog with matching uuid does exist in enterprise-catalog
            # service, so we should update the existing catalog
            update_fields = {
                'enterprise_customer': str(instance.enterprise_customer.uuid),
                'enterprise_customer_name': instance.enterprise_customer.name,
                'title': instance.title,
                'content_filter': instance.content_filter,
                'enabled_course_modes': instance.enabled_course_modes,
                'publish_audit_enrollment_urls': instance.publish_audit_enrollment_urls,
                'catalog_query_uuid': catalog_query_uuid,
                'query_title': query_title,
                'include_exec_ed_2u_courses': include_exec_ed_2u_courses,
            }
            catalog_client.update_enterprise_catalog(catalog_uuid, **update_fields)
        # Refresh catalog on all creates and updates
        catalog_client.refresh_catalogs([instance])


@receiver(post_delete, sender=models.EnterpriseCustomerCatalog)
def delete_enterprise_catalog_data(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Send deletions of Enterprise Catalogs to the Enterprise Catalog Service.
    """
    catalog_uuid = instance.uuid
    try:
        catalog_client = EnterpriseCatalogApiClient()
        catalog_client.delete_enterprise_catalog(catalog_uuid)
    except NotConnectedToOpenEdX as exc:
        logger.exception(
            'Unable to delete Enterprise Catalog {}'.format(str(catalog_uuid)),
            exc_info=exc
        )

    customer = instance.enterprise_customer
    for channel in INTEGRATED_CHANNELS:
        if channel.objects.filter(enterprise_customer=customer, active=True).exists():
            logger.info(
                f"Catalog {catalog_uuid} deletion is linked to an active integrated channels config, running the mark"
                f"orphan content audits task"
            )
            mark_orphaned_content_metadata_audit.delay()
            break


def course_enrollment_changed_receiver(sender, **kwargs):     # pylint: disable=unused-argument
    """
    Handle when a course enrollment is (de/re)activated.

    Importantly, if a student.CourseEnrollment is being reactivated, take this opportunity to atomically reactivate
    the corresponding EnterpriseCourseEnrollment.
    """
    enrollment = kwargs.get('enrollment')
    enterprise_enrollment = models.EnterpriseCourseEnrollment.objects.filter(
        course_id=enrollment.course.course_key,
        enterprise_customer_user__user_id=enrollment.user.id,
    ).first()
    if enterprise_enrollment and enrollment.is_active:
        enterprise_enrollment.set_unenrolled(False)
    # Note: If the CourseEnrollment is being flipped to is_active=False, then this handler is a no-op.
    # In that case, the `enterprise_unenrollment_receiver` signal handler below will run.


def enterprise_unenrollment_receiver(sender, **kwargs):     # pylint: disable=unused-argument
    """
    Mark the EnterpriseCourseEnrollment object as unenrolled when a user unenrolls from a course.
    """
    enrollment = kwargs.get('enrollment')
    enterprise_enrollment = models.EnterpriseCourseEnrollment.objects.filter(
        course_id=enrollment.course.course_key,
        enterprise_customer_user__user_id=enrollment.user.id,
    ).first()
    if enterprise_enrollment:
        enterprise_enrollment.set_unenrolled(True)


def create_enterprise_enrollment_receiver(sender, instance, **kwargs):     # pylint: disable=unused-argument
    """
    Watches for post_save signal for creates on the CourseEnrollment table.

    Spin off an async task to generate an EnterpriseCourseEnrollment if appropriate.
    """
    if kwargs.get('created') and instance.user:
        user_id = instance.user.id
        # NOTE: there should be _at most_ 1 EnterpriseCustomerUser record  with `active=True`
        active_ecus_for_user = models.EnterpriseCustomerUser.objects.filter(user_id=user_id, active=True)
        ecu = active_ecus_for_user.first()
        if not ecu:
            # nothing to do here
            return
        if len(active_ecus_for_user) > 1:
            logger.warning(
                'User %s has more than 1 active EnterpriseCustomerUser object. Continuing with course enrollment'
                'for course %s but the enrollment may end up associated with an incorrect EnterpriseCustomerUser',
                user_id,
                instance.course_id,
            )

        # Number of seconds to tell celery to wait before the `create_enterprise_enrollment`
        # task should begin execution.
        countdown = getattr(settings, 'CREATE_ENTERPRISE_ENROLLMENT_TASK_COUNTDOWN', DEFAULT_COUNTDOWN)

        def submit_task():
            """
            In-line helper to run the create_enterprise_enrollment task on commit.
            """
            logger.info((
                "User %s is an EnterpriseCustomerUser. Spinning off task to check if course is within User's "
                "Enterprise's EnterpriseCustomerCatalog."
            ), user_id)
            task_args = (str(instance.course_id), ecu.id)
            # Submit the task with a countdown to help avoid possible race-conditions/deadlocks
            # due to external processes that read or write the same
            # records the task tries to read or write.
            create_enterprise_enrollment.apply_async(task_args, countdown=countdown)

        # This receiver might be executed within a transaction that creates an ECE record.
        # Ensure that the task is only submitted after a commit tasks place, because
        # the task first checks if that ECE record exists and exits early, which we want it
        # to do before later attempting to *create* the same record (which could lead to a race-condition error).
        transaction.on_commit(submit_task)


@receiver(pre_save, sender=models.EnterpriseCustomerSsoConfiguration)
def generate_default_orchestration_record_display_name(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """
    Ensure that the display_name field is populated with a default value if it is not provided while creating.
    """
    if not models.EnterpriseCustomerSsoConfiguration.objects.filter(pk=instance.pk).exists():
        if instance.display_name is None:
            num_records_for_customer = models.EnterpriseCustomerSsoConfiguration.objects.filter(
                enterprise_customer=instance.enterprise_customer,
            ).count()
            instance.display_name = f'SSO-config-{instance.identity_provider}-{num_records_for_customer + 1}'


# Don't connect this receiver if we dont have access to CourseEnrollment model
if CourseEnrollment is not None:
    post_save.connect(create_enterprise_enrollment_receiver, sender=CourseEnrollment)

if COURSE_UNENROLLMENT_COMPLETED is not None:
    COURSE_UNENROLLMENT_COMPLETED.connect(enterprise_unenrollment_receiver)

if COURSE_ENROLLMENT_CHANGED is not None:
    COURSE_ENROLLMENT_CHANGED.connect(course_enrollment_changed_receiver)
