%global  lrdname  leapp-repository-deps-el9
%global  ldname   leapp-deps-el9

Name:       leapp-el8toel9-deps
Version:    5.0.0
Release:    1%{?dist}
Summary:    Dependencies for *leapp* packages
BuildArch:  noarch

License:    ASL 2.0
URL:        https://oamg.github.io/leapp/

%description
%{summary}

##################################################
# DEPS FOR LEAPP REPOSITORY ON RHEL 9
##################################################
# TODO: MODIFY AS YOU WISH
%package -n %{lrdname}
Summary:    Meta-package with system dependencies for leapp repository
Provides:   leapp-repository-dependencies = 5
Obsoletes:  leapp-repository-deps

Requires:   dnf >= 4
Requires:   pciutils
Requires:   python3
Requires:   python3-pyudev
# required by SELinux actors
Requires:   policycoreutils-python-utils

%description -n %{lrdname}
%{summary}

##################################################
# DEPS FOR LEAPP FRAMEWORK ON RHEL 9
##################################################
# TODO: modify as you with...
%package -n %{ldname}
Summary:    Meta-package with system dependencies for leapp framework
Provides:   leapp-framework-dependencies = 3
Obsoletes:  leapp-deps

Requires:   findutils

Requires:   python3-six
Requires:   python3-setuptools
Requires:   python3-requests

%description -n %{ldname}
%{summary}

%prep

%build

%install

# do not create main packages
#%files

%files -n %{lrdname}
# no files here

%files -n %{ldname}
# no files here

%changelog
* Tue Jan 22 2019 Petr Stodulka <pstodulk@redhat.com> - %{version}-%{release}
- Initial rpm
