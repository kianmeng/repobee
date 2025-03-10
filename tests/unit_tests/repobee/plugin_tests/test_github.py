import itertools
import random
import datetime
from unittest.mock import MagicMock, PropertyMock, patch
from typing import List, NoReturn

import pytest
import github
import responses
from _repobee import http

import repobee_plug as plug

import _repobee.ext.defaults.github as github_plugin
from _repobee.ext.defaults.github import (
    REQUIRED_TOKEN_SCOPES,
    _ISSUE_STATE_MAPPING,
)

from repobee_testhelpers._internal import constants
from repobee_testhelpers._internal.constants import (
    USER,
    ORG_NAME,
    TOKEN,
    BASE_URL,
)


class GithubException(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.msg = msg
        self.status = status


NOT_FOUND_EXCEPTION = GithubException(msg=None, status=404)
VALIDATION_ERROR = GithubException(msg=None, status=422)
SERVER_ERROR = GithubException(msg=None, status=500)

NOT_MEMBER = "notanowner"


def random_date():
    return constants.FIXED_DATETIME - datetime.timedelta(
        days=random.randint(0, 1000),
        hours=random.randint(0, 1000),
        minutes=random.randint(0, 1000),
        seconds=random.randint(0, 1000),
    )


def generate_repo_url(repo_name, org_name):
    return f"{constants.HOST_URL}/{org_name}/{repo_name}"


def to_magic_mock_issue(issue):
    """Convert an issue to a MagicMock with all of the correct
    attribuets."""
    mock = MagicMock(spec=github.Issue.Issue)
    mock.user = MagicMock()
    mock.title = issue.title
    mock.body = issue.body
    mock.created_at = datetime.datetime.fromisoformat(issue.created_at)
    mock.number = issue.number
    mock.user = constants.User(issue.author)
    mock.state = _ISSUE_STATE_MAPPING[issue.state]
    return mock


def from_magic_mock_issue(mock_issue):
    """Convert a MagicMock issue into a plug.Issue."""
    return plug.Issue(
        title=mock_issue.title,
        body=mock_issue.body,
        number=mock_issue.number,
        created_at=mock_issue.created_at,
        author=mock_issue.user.login,
    )


User = constants.User


def raise_404(*args, **kwargs) -> NoReturn:
    raise GithubException("Couldn't find something", 404)


def raise_422(*args, **kwargs) -> NoReturn:
    raise GithubException("Already exists", 422)


def raise_401(*args, **kwargs) -> NoReturn:
    raise GithubException("Access denied", 401)


@pytest.fixture
def review_student_teams():
    return [
        plug.StudentTeam(members=[student])
        for student in ("ham", "spam", "bacon", "eggs")
    ]


@pytest.fixture(autouse=True)
def mock_github(mocker):
    return mocker.patch("github.Github", autospec=True)


@pytest.fixture
def review_teams(review_student_teams):
    master_repo = "week-1"
    review_teams = {}
    for i, student_team in enumerate(review_student_teams):
        review_teams[
            plug.generate_review_team_name(student_team, master_repo)
        ] = itertools.chain.from_iterable(
            team.members
            for team in review_student_teams[:i]
            + review_student_teams[i + 1 :]
        )
    return review_teams


@pytest.fixture
def teams_and_members(review_teams):
    """Fixture with a dictionary contain a few teams with member lists."""
    return {
        "one": ["first", "second"],
        "two": ["two"],
        "last_team": [str(i) for i in range(10)],
        **review_teams,
    }


@pytest.fixture
def happy_github(mocker, monkeypatch, teams_and_members):
    """mock of github.Github which raises no exceptions and returns the
    correct values.
    """
    github_instance = MagicMock()
    github_instance.get_user.side_effect = (
        lambda user: User(login=user)
        if user in [USER, NOT_MEMBER]
        else raise_404()
    )
    type(github_instance).oauth_scopes = PropertyMock(
        return_value=REQUIRED_TOKEN_SCOPES
    )

    usernames = set(
        itertools.chain(*[members for _, members in teams_and_members.items()])
    )

    def get_user(username):
        if username in [*usernames, USER, NOT_MEMBER]:
            user = MagicMock(spec=github.NamedUser.NamedUser)
            type(user).login = PropertyMock(return_value=username)
            return user
        else:
            raise_404()

    github_instance.get_user.side_effect = get_user
    monkeypatch.setattr(github, "GithubException", GithubException)
    mocker.patch(
        "github.Github",
        side_effect=lambda login_or_token, base_url: github_instance,
    )

    return github_instance


@pytest.fixture
def organization(happy_github):
    """Attaches an Organization mock to github.Github.get_organization, and
    returns the mock.
    """
    return create_mock_organization(
        happy_github, ORG_NAME, ["blablabla", "hello", USER]
    )


def create_mock_organization(
    github_impl: MagicMock, org_name: str, members: List[str]
) -> MagicMock:
    organization = MagicMock()

    def get_members(role=""):
        return [User(login=member) for member in members]

    organization.get_members = get_members
    type(organization).html_url = PropertyMock(
        return_value=generate_repo_url("", org_name).rstrip("/")
    )
    github_impl.get_organization.side_effect = (
        lambda n: organization if n == org_name else raise_404()
    )

    return organization


def mock_team(name):
    """create a mock team that tracks its members."""
    team = MagicMock()
    members = set()
    team.get_members.side_effect = lambda: list(members)
    team.add_membership.side_effect = members.add
    type(team).name = PropertyMock(return_value=name)
    type(team).id = PropertyMock(return_value=hash(name))
    return team


@pytest.fixture
def no_teams(organization):
    """A fixture that sets up the teams functionality without adding any
    teams.
    """
    ids_to_teams = {}
    organization.get_team.side_effect = (
        lambda team_id: ids_to_teams[team_id]
        if team_id in ids_to_teams
        else raise_404()
    )
    organization.get_teams.side_effect = lambda: list(teams_)
    teams_ = []

    def create_team(name, permission):
        nonlocal teams_, ids_to_teams

        assert permission in ["push", "pull"]
        if name in [team.name for team in teams_]:
            raise_422()

        team = mock_team(name)
        ids_to_teams[team.id] = team
        teams_.append(team)
        return team

    organization.create_team.side_effect = create_team
    return teams_


@pytest.fixture
def teams(organization, no_teams, teams_and_members):
    """A fixture that returns a list of teams, which are all returned by the
    github.Organization.Organization.get_teams function."""
    team_names = teams_and_members.keys()
    for name in team_names:
        organization.create_team(name, permission="push")
    return no_teams  # the list of teams!


def mock_repo(name, description, private, team_id):
    repo = MagicMock()
    type(repo).name = PropertyMock(return_value=name)
    type(repo).description = PropertyMock(
        return_value=f"description of {name}"
    )
    type(repo).html_url = PropertyMock(
        return_value=generate_repo_url(name, ORG_NAME)
    )
    # repo.get_teams.side_effect = lambda: [team]
    return repo


@pytest.fixture
def no_repos(teams_and_members, teams, organization):
    repos_in_org = {}

    def get_repo(repo_name):
        if repo_name in repos_in_org:
            return repos_in_org[repo_name]
        raise NOT_FOUND_EXCEPTION

    def create_repo(name, description="", private=True, team_id=None):
        nonlocal repos_in_org
        if name in repos_in_org:
            raise VALIDATION_ERROR
        repo = mock_repo(name, description, private, team_id)
        repos_in_org[name] = repo
        return repo

    organization.create_repo.side_effect = create_repo
    organization.get_repo.side_effect = get_repo
    organization.get_repos.side_effect = lambda: list(repos_in_org.values())


@pytest.fixture
def repos(organization, no_repos, teams_and_members, teams):
    for team in teams:
        organization.create_repo(
            f"{team.name}-week-2",
            description=f"A nice repo for {team.name}",
            private=True,
        )
    return organization.get_repos()


@pytest.fixture(scope="function")
def api(happy_github, organization, no_teams):
    return _create_github_api_without_rate_limiting(
        BASE_URL, TOKEN, ORG_NAME, USER
    )


class TestInit:
    def test_raises_on_empty_user_arg(self):
        with pytest.raises(TypeError) as exc_info:
            _create_github_api_without_rate_limiting(
                BASE_URL, TOKEN, ORG_NAME, ""
            )

        assert "argument 'user' must not be empty" in str(exc_info.value)

    @pytest.mark.parametrize("url", ["https://github.com", constants.HOST_URL])
    def test_raises_when_url_is_bad(self, url):
        with pytest.raises(plug.PlugError) as exc_info:
            _create_github_api_without_rate_limiting(
                url, TOKEN, ORG_NAME, USER
            )

        assert (
            "invalid base url, should either be https://api.github.com or "
            "end with '/api/v3'" in str(exc_info.value)
        )

    @pytest.mark.parametrize(
        "url", ["https://api.github.com", constants.BASE_URL]
    )
    def test_accepts_valid_urls(self, url):
        api = _create_github_api_without_rate_limiting(
            url, TOKEN, ORG_NAME, USER
        )

        assert isinstance(api, plug.PlatformAPI)


class TestGetRepos:
    """Tests for get_repos."""

    def test_get_all_repos(self, api, repos):
        """Calling get_repos without an argument should return all repos."""
        assert len(list(api.get_repos())) == len(repos)


class TestDeleteRepo:
    """Tests for delete_repo."""

    def test_delete_repo(self, api):
        platform_repo = MagicMock()
        repo = plug.Repo(
            "some-repo",
            "Some description",
            True,
            "url-doesnt-matter",
            implementation=platform_repo,
        )

        api.delete_repo(repo)

        platform_repo.delete.assert_called_once()


class TestInsertAuth:
    """Tests for insert_auth."""

    def test_inserts_into_https_url(self, api):
        url = f"{BASE_URL}/some/repo"
        authed_url = api.insert_auth(url)
        assert authed_url.startswith(f"https://{USER}:{TOKEN}")

    def test_raises_on_non_platform_url(self, api):
        url = "https://somedomain.com"

        with pytest.raises(plug.InvalidURL) as exc_info:
            api.insert_auth(url)

        assert "url not found on platform" in str(exc_info.value)

    def test_retains_endpoint(self, api):
        endpoint = "some/repo"
        url = f"{BASE_URL}/some/repo"
        authed_url = api.insert_auth(url)
        assert endpoint in authed_url


class TestGetRepoUrls:
    """Tests for get_repo_urls."""

    def test_with_token_and_user(self, repos, api):
        repo_names = [repo.name for repo in repos]
        api._user = USER
        expected_urls = [api.insert_auth(repo.html_url) for repo in repos]

        urls = api.get_repo_urls(repo_names, insert_auth=True)

        assert sorted(urls) == sorted(expected_urls)
        for url in urls:
            assert f"{USER}:{TOKEN}" in url

    def test_with_students(self, repos, api):
        """Test that supplying students causes student repo names to be
        generated as the Cartesian product of the supplied repo names and the
        students.
        """
        students = list(constants.STUDENTS)
        assignment_names = [repo.name for repo in repos]
        expected_repo_names = plug.generate_repo_names(
            students, assignment_names
        )
        # assume works correctly when called with just repo names
        expected_urls = api.get_repo_urls(expected_repo_names)

        actual_urls = api.get_repo_urls(
            assignment_names, team_names=[t.name for t in students]
        )

        assert len(actual_urls) == len(students) * len(assignment_names)
        assert sorted(expected_urls) == sorted(actual_urls)


@pytest.fixture(params=["get_user", "get_organization"])
def github_bad_info(request, api, happy_github):
    """Fixture with a github instance that raises GithubException 404 when
    use the user, base_url and org_name arguments to .
    """
    getattr(happy_github, request.param).side_effect = raise_404
    return happy_github


class TestVerifySettings:
    """Tests for the verify_settings function."""

    @responses.activate
    def test_happy_path(
        self,
        happy_github,
        organization,
        api,
        add_internet_connection_check_response,
    ):
        """Tests that no exceptions are raised when all info is correct."""
        github_plugin.GitHubAPI.verify_settings(
            USER, ORG_NAME, BASE_URL, TOKEN
        )

    @responses.activate
    def test_raises_on_no_internet_connection(self):
        with pytest.raises(plug.InternetConnectionUnavailable) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, token=TOKEN
            )

        assert "could not establish an Internet connection" in str(exc_info)

    @responses.activate
    def test_empty_token_raises_bad_credentials(
        self,
        happy_github,
        monkeypatch,
        api,
        add_internet_connection_check_response,
    ):
        with pytest.raises(plug.BadCredentials) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, ""
            )

        assert "token is empty" in str(exc_info.value)

    @responses.activate
    def test_incorrect_info_raises_not_found_error(
        self, github_bad_info, api, add_internet_connection_check_response
    ):
        with pytest.raises(plug.NotFoundError):
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, TOKEN
            )

    @responses.activate
    def test_bad_token_scope_raises(
        self, happy_github, api, add_internet_connection_check_response
    ):
        type(happy_github).oauth_scopes = PropertyMock(return_value=["repo"])

        with pytest.raises(plug.BadCredentials) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, TOKEN
            )
        assert "missing one or more access token scopes" in str(exc_info.value)

    @responses.activate
    def test_not_member_raises(
        self,
        happy_github,
        organization,
        api,
        add_internet_connection_check_response,
    ):
        with pytest.raises(plug.BadCredentials) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                NOT_MEMBER, ORG_NAME, BASE_URL, TOKEN
            )

        assert f"user {NOT_MEMBER} is not a member" in str(exc_info.value)

    @responses.activate
    def test_not_owner_warning(
        self,
        happy_github,
        organization,
        mocker,
        api,
        add_internet_connection_check_response,
    ):
        warn_mock = mocker.patch("repobee_plug.log.warning")
        orig_get_members = organization.get_members

        def get_members(role=""):
            if role == "admin":
                return []
            else:
                return orig_get_members(role)

        organization.get_members = get_members

        github_plugin.GitHubAPI.verify_settings(
            USER, ORG_NAME, BASE_URL, TOKEN
        )

        warn_mock.assert_any_call(
            f"{USER} is not an owner of {ORG_NAME}. "
            "Some features may not be available."
        )

    @responses.activate
    def test_raises_unexpected_exception_on_unexpected_status(
        self, happy_github, api, add_internet_connection_check_response
    ):
        happy_github.get_user.side_effect = SERVER_ERROR
        with pytest.raises(plug.UnexpectedException):
            api.verify_settings(USER, ORG_NAME, BASE_URL, TOKEN)

    @responses.activate
    def test_none_user_raises(
        self,
        happy_github,
        organization,
        api,
        add_internet_connection_check_response,
    ):
        """If NamedUser.login is None, there should be an exception. Happens if
        you provide a URL that points to a GitHub instance, but not to the API
        endpoint.
        """
        happy_github.get_user.side_effect = lambda _: User(login=None)

        with pytest.raises(plug.UnexpectedException) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, TOKEN
            )

        assert "Possible reasons: bad api url" in str(exc_info.value)

    @responses.activate
    def test_mismatching_user_login_raises(
        self,
        happy_github,
        organization,
        api,
        add_internet_connection_check_response,
    ):
        """I'm not sure if this can happen, but since the None-user thing
        happened, better safe than sorry.
        """
        wrong_username = USER + "other"
        happy_github.get_user.side_effect = lambda username: User(
            username + "other"
        )
        expected_messages = [
            f"Specified login is {USER}, but the fetched user's login "
            f"is {wrong_username}. "
            "Possible reasons: unknown",
        ]

        with pytest.raises(plug.UnexpectedException) as exc_info:
            github_plugin.GitHubAPI.verify_settings(
                USER, ORG_NAME, BASE_URL, TOKEN
            )

        for msg in expected_messages:
            assert msg in str(exc_info.value)


class TestGetRepoIssues:
    """Tests for the get_repo_issues function."""

    _ARBITRARY_ISSUE = plug.Issue(
        title="Arbitrary title",
        body="Arbitrary body",
        number=1,
        created_at="2022-01-01",
        state=plug.IssueState.OPEN,
        author="slarse",
    )

    def test_fetches_all_issues(self, happy_github, api):
        impl_mock = MagicMock(spec=github.Repository.Repository)
        repo = plug.Repo(
            name="name",
            description="descr",
            private=True,
            url="bla",
            implementation=impl_mock,
        )

        api.get_repo_issues(repo)

        impl_mock.get_issues.assert_called_once_with(state="all")

    def test_replaces_none_body_with_empty_string(self, happy_github, api):
        repo_implementation_mock = MagicMock(spec=github.Repository.Repository)
        repo = plug.Repo(
            name="name",
            description="descr",
            private=True,
            url="bla",
            implementation=repo_implementation_mock,
        )
        issue_mock = to_magic_mock_issue(self._ARBITRARY_ISSUE)
        issue_mock.body = None

        repo_implementation_mock.get_issues.side_effect = (
            lambda *args, **kwargs: [issue_mock]
        )

        issue, *rest = api.get_repo_issues(repo)

        assert not rest, "did not expect more issues"
        assert issue.body == ""


class TestCreateIssue:
    """Tests for the create_issue function."""

    def test_sets_assignees_defaults_to_notset(self, happy_github, api):
        """Assert that ``assignees = None`` is replaced with ``NotSet``."""
        impl_mock = MagicMock(spec=github.Repository.Repository)
        repo = plug.Repo(
            name="name",
            description="descr",
            private=True,
            url="bla",
            implementation=impl_mock,
        )

        with patch(
            "_repobee.ext.defaults.github.GitHubAPI._wrap_issue", autospec=True
        ):
            api.create_issue("Title", "Body", repo)

        impl_mock.create_issue.assert_called_once_with(
            "Title", body="Body", assignees=github.GithubObject.NotSet
        )


class TestForOrganization:
    """Tests for the for_organization function."""

    def test_correctly_sets_provided_org(self, happy_github, api):
        """Test that the provided organization is respected."""
        new_org_name = "some-other-org"
        assert (
            api._org_name != new_org_name
        ), "test makes no sense if the new org name matches the existing one"
        mock_org = create_mock_organization(happy_github, new_org_name, [])

        new_api = api.for_organization(new_org_name)

        assert new_api.org is mock_org


def _create_github_api_without_rate_limiting(*args, **kwargs):
    """The rate limiting implementation interferes with responses, so we remove
    them for unit tests.
    """
    api = github_plugin.GitHubAPI(*args, **kwargs)
    http.remove_rate_limits()
    return api
