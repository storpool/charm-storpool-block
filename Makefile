#!/usr/bin/make -f

NAME?=		storpool-block
SERIES?=	xenial

SRCS=		\
		icon.svg \
		layer.yaml \
		metadata.yaml \
		\
		reactive/storpool-block-charm.py \


BUILDDIR=	${CURDIR}/../built/${SERIES}/${NAME}
TARGETDIR=	${BUILDDIR}/${SERIES}/${NAME}
BUILD_MANIFEST=	${TARGETDIR}/.build.manifest

all:	charm

charm:	${BUILD_MANIFEST}

${BUILD_MANIFEST}:	${SRCS}
	charm build -s '${SERIES}' -n '${NAME}' -o '${BUILDDIR}'

clean:
	rm -rf -- '${TARGETDIR}'

deploy:	all
	juju deploy --to 2 -- '${TARGETDIR}'

upgrade:	all
	juju upgrade-charm --path '${TARGETDIR}' -- '${NAME}'

.PHONY:	all charm clean deploy upgrade
